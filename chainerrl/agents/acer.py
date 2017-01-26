from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals
from __future__ import absolute_import
from builtins import *  # NOQA
from future import standard_library
standard_library.install_aliases()

import contextlib
import copy
from logging import getLogger
import os

import chainer
from chainer import functions as F
from chainer import serializers
import numpy as np

from chainerrl import agent
from chainerrl.misc import async
from chainerrl.misc import copy_param
from chainerrl.misc.makedirs import makedirs
from chainerrl.recurrent import Recurrent
from chainerrl.recurrent import RecurrentChainMixin
from chainerrl.recurrent import state_kept
from chainerrl.recurrent import state_reset

logger = getLogger(__name__)


class ACERSeparateModel(chainer.Chain, RecurrentChainMixin):
    """ACER model that consists of a separate policy and V-function.

    Args:
        pi (Policy): Policy.
        q (QFunction): Q-function.
    """

    def __init__(self, pi, q):
        super().__init__(pi=pi, q=q)

    def __call__(self, obs):
        pout = self.pi(obs)
        qout = self.q(obs)
        return pout, qout


class ACERSharedModel(chainer.Chain, RecurrentChainMixin):
    """ACER model where the policy and V-function share parameters.

    Args:
        shared (Link): Shared part. Nonlinearity must be included in it.
        pi (Policy): Policy that receives output of shared as input.
        q (QFunction): Q-function that receives output of shared as input.
    """

    def __init__(self, shared, pi, q):
        super().__init__(shared=shared, pi=pi, q=q)

    def __call__(self, obs):
        h = self.shared(obs)
        pout = self.pi(h)
        qout = self.q(h)
        return pout, qout


def compute_discrete_kl(p, q):
    return F.sum(p.all_prob * (p.all_log_prob - q.all_log_prob), axis=1)


@contextlib.contextmanager
def backprop_truncated(variable):
    backup = variable.creator
    variable.creator = None
    yield
    variable.creator = backup


class DiscreteACER(agent.AsyncAgent):
    """Discrete ACER (Actor-Critic with Experience Replay).

    See http://arxiv.org/abs/1611.01224

    Args:
        model (ACERModel): Model to train
        optimizer (chainer.Optimizer): optimizer used to train the model
        t_max (int): The model is updated after every t_max local steps
        gamma (float): Discount factor [0,1]
        beta (float): Weight coefficient for the entropy regularizaiton term.
        phi (callable): Feature extractor function
        pi_loss_coef (float): Weight coefficient for the loss of the policy
        v_loss_coef (float): Weight coefficient for the loss of the value
            function
        normalize_loss_by_steps (bool): If set true, losses are normalized by
            the number of steps taken to accumulate the losses
        act_deterministically (bool): If set true, choose most probable actions
            in act method.
        average_entropy_decay (float): Decay rate of average entropy. Used only
            to record statistics.
        average_value_decay (float): Decay rate of average value. Used only
            to record statistics.
    """

    process_idx = None

    def __init__(self, model, optimizer, t_max, gamma, replay_buffer,
                 beta=1e-2,
                 process_idx=0, phi=lambda x: x,
                 pi_loss_coef=1.0, v_loss_coef=0.5,
                 trust_region_alpha=0.99,
                 trust_region_c=10,
                 trust_region_delta=1,
                 n_times_replay=8,
                 normalize_loss_by_steps=True,
                 act_deterministically=False,
                 average_entropy_decay=0.999,
                 average_value_decay=0.999):

        # Globally shared model
        self.shared_model = model

        # Globally shared average model used to compute trust regions
        self.shared_average_model = copy.deepcopy(self.shared_model)

        # Thread specific model
        self.model = copy.deepcopy(self.shared_model)
        async.assert_params_not_shared(self.shared_model, self.model)

        self.optimizer = optimizer

        self.replay_buffer = replay_buffer
        self.t_max = t_max
        self.gamma = gamma
        self.beta = beta
        self.phi = phi
        self.pi_loss_coef = pi_loss_coef
        self.v_loss_coef = v_loss_coef
        self.normalize_loss_by_steps = normalize_loss_by_steps
        self.act_deterministically = act_deterministically
        self.trust_region_alpha = trust_region_alpha
        self.trust_region_c = trust_region_c
        self.trust_region_delta = trust_region_delta
        self.n_times_replay = n_times_replay
        self.average_value_decay = average_value_decay
        self.average_entropy_decay = average_entropy_decay

        self.t = 0
        self.t_start = 0
        self.past_action_log_prob = {}
        self.past_action_entropy = {}
        self.past_states = {}
        self.past_rewards = {}
        self.past_values = {}
        self.past_action_distrib = {}
        self.past_avg_action_distrib = {}
        self.last_state = None
        self.last_action = None
        # ACER won't use a explorer, but this arrtibute is referenced by
        # run_dqn
        self.explorer = None

        # Stats
        self.average_value = 0
        self.average_entropy = 0

    def sync_parameters(self):
        copy_param.copy_param(target_link=self.model,
                              source_link=self.shared_model)
        copy_param.soft_copy_param(target_link=self.shared_average_model,
                                   source_link=self.model,
                                   tau=1 - self.trust_region_alpha)

    @property
    def shared_attributes(self):
        return ('shared_model', 'shared_average_model', 'optimizer')

    def update(self, t_start, t_stop, R, states, actions, rewards, values, action_values,
               action_log_probs, action_entropy,
               action_distribs, avg_action_distribs, rho=None, rho_a=None, rho_bar=None):

        pi_loss = 0
        v_loss = 0
        Q_ret = R
        del R
        for i in reversed(range(t_start, t_stop)):
            Q_ret = rewards[i] + self.gamma * Q_ret
            v = values[i]
            if self.process_idx == 0:
                logger.debug('t:%s s:%s v:%s Q_ret:%s',
                             i, states[i].sum(), v.data, Q_ret)
            advantage = Q_ret - v
            log_prob = action_log_probs[i]
            entropy = action_entropy[i]
            action_distrib = action_distribs[i]

            # Compute gradients w.r.t statistics produced by the model
            with backprop_truncated(action_distrib.logits):

                # Compute g
                if rho is not None:
                    # Off-policy
                    rho_bar = min(self.trust_region_c, rho[i])
                    g_loss = 0
                    g_loss += rho_bar * log_prob * float(advantage.data)
                    correction_weight = np.maximum(
                        (rho_a[i] - self.trust_region_c) / rho_a[i],
                        np.zeros_like(rho_a[i]))
                    with chainer.no_backprop_mode():
                        correction_advantage = (action_values[i].q_values.data -
                                                values[i].data)
                    g_loss += F.sum(correction_weight *
                                    action_distribs[i].all_log_prob *
                                    correction_advantage, axis=1)
                else:
                    # On-policy
                    g_loss = log_prob * float(advantage.data)
                g_loss.backward()
                g = action_distrib.logits.grad[0]
                action_distrib.logits.grad = None

                # Compute k
                kl = compute_discrete_kl(
                    avg_action_distribs[i],
                    action_distribs[i])
                kl.backward()
                k = action_distrib.logits.grad[0]
                action_distrib.logits.grad = None

            # Compute gradients w.r.t parameters of the model
            z = (g -
                 max(0, ((np.dot(k, g) - self.trust_region_delta) /
                         np.dot(k, k))))
            pi_loss -= F.sum(action_distrib.logits * z, axis=1)
            # Entropy is maximized
            pi_loss -= self.beta * entropy
            # Accumulate gradients of value function
            v_loss += (Q_ret - v) ** 2 / 2

            ba = np.expand_dims(actions[i], 0)
            Q = float(action_values[i].evaluate_actions(ba).data)
            Q_ret = min(1, rho[i]) * (Q_ret - Q) + float(values[i].data)

        pi_loss *= self.pi_loss_coef
        v_loss *= self.v_loss_coef

        if self.normalize_loss_by_steps:
            pi_loss /= t_stop - t_start
            v_loss /= t_stop - t_start

        if self.process_idx == 0:
            logger.debug('pi_loss:%s v_loss:%s', pi_loss.data, v_loss.data)

        total_loss = pi_loss + F.reshape(v_loss, pi_loss.data.shape)

        # Compute gradients using thread-specific model
        self.model.zerograds()
        total_loss.backward()
        # Copy the gradients to the globally shared model
        self.shared_model.zerograds()
        copy_param.copy_grad(
            target_link=self.shared_model, source_link=self.model)
        # Update the globally shared model
        if self.process_idx == 0:
            norm = self.optimizer.compute_grads_norm()
            logger.debug('grad norm:%s', norm)
        self.optimizer.update()
        if self.process_idx == 0:
            logger.debug('update')

        self.sync_parameters()
        if isinstance(self.model, Recurrent):
            self.model.unchain_backward()

    def update_from_replay(self):

        if len(self.replay_buffer) == 0:
            return

        episode = self.replay_buffer.sample_episodes(1, self.t_max + 1)[0]
        # print('episode', episode)

        with state_reset(self.model):
            with state_reset(self.shared_average_model):
                rewards = {}
                states = {}
                actions = {}
                action_log_probs = {}
                action_entropy = {}
                action_distribs = {}
                avg_action_distribs = {}
                rho = {}
                rho_a = {}
                action_values = {}
                values = {}
                for t, transition in enumerate(episode):
                    s = self.phi(transition['state'])
                    a = transition['action']
                    ba = np.expand_dims(a, 0)
                    bs = np.expand_dims(s, 0)
                    action_distrib, action_value = self.model(bs)
                    v = F.sum(action_distrib.all_prob *
                              action_value.q_values, axis=1)
                    with chainer.no_backprop_mode():
                        avg_action_distrib, _ = self.shared_average_model(bs)
                    states[t] = s
                    actions[t] = a
                    action_log_probs[t] = action_distrib.log_prob(ba)
                    action_entropy[t] = action_distrib.entropy
                    values[t] = v
                    action_distribs[t] = action_distrib
                    avg_action_distribs[t] = avg_action_distrib
                    rewards[t] = transition['reward']
                    mu = transition['mu']
                    action_values[t] = action_value
                    rho[t] = action_distrib.prob(ba).data / mu.prob(ba).data
                    rho_a[t] = action_distrib.all_prob.data / mu.all_prob.data
                last_transition = episode[-1]
                if last_transition['is_state_terminal']:
                    R = 0
                else:
                    with chainer.no_backprop_mode():
                        last_s = last_transition['next_state']
                        action_distrib, action_value = self.model(
                            np.expand_dims(self.phi(last_s), 0))
                        last_v = F.sum(action_distrib.all_prob *
                                       action_value.q_values, axis=1)
                    R = float(last_v.data)
                return self.update(
                    R=R, t_start=0, t_stop=len(episode),
                    states=states, rewards=rewards,
                    actions=actions,
                    values=values,
                    action_log_probs=action_log_probs,
                    action_entropy=action_entropy,
                    action_distribs=action_distribs,
                    avg_action_distribs=avg_action_distribs,
                    rho=rho,
                    rho_a=rho_a,
                    action_values=action_values)

    def update_on_policy(self, statevar):
        assert self.t_start < self.t

        if statevar is None:
            R = 0
        else:
            with chainer.no_backprop_mode():
                with state_kept(self.model):
                    action_distrib, action_value = self.model(statevar)
                    v = F.sum(action_distrib.all_prob *
                              action_value.q_values, axis=1)
            R = float(v.data)

        pi_loss = 0
        v_loss = 0
        for i in reversed(range(self.t_start, self.t)):
            R *= self.gamma
            R += self.past_rewards[i]
            v = self.past_values[i]
            if self.process_idx == 0:
                logger.debug('t:%s s:%s v:%s R:%s',
                             i, self.past_states[i].sum(), v.data, R)
            advantage = R - v
            # Accumulate gradients of policy
            log_prob = self.past_action_log_prob[i]
            entropy = self.past_action_entropy[i]
            action_distrib = self.past_action_distrib[i]

            # Compute gradients w.r.t statistics produced by the model
            with backprop_truncated(action_distrib.logits):
                # Compute g
                g_loss = log_prob * float(advantage.data)
                g_loss.backward()
                g = action_distrib.logits.grad[0]
                action_distrib.logits.grad = None
                # Compute k
                kl = compute_discrete_kl(
                    self.past_avg_action_distrib[i],
                    self.past_action_distrib[i])
                kl.backward()
                k = action_distrib.logits.grad[0]
                action_distrib.logits.grad = None
            # Compute gradients w.r.t parameters of the model
            # print('k', k)
            # print('g', g)
            z = (g -
                 max(0, ((np.dot(k, g) - self.trust_region_delta) /
                         np.dot(k, k))))
            pi_loss -= F.sum(action_distrib.logits * z, axis=1)
            # Entropy is maximized
            pi_loss -= self.beta * entropy
            # Accumulate gradients of value function
            v_loss += (v - R) ** 2 / 2

        pi_loss *= self.pi_loss_coef
        v_loss *= self.v_loss_coef

        if self.normalize_loss_by_steps:
            pi_loss /= self.t - self.t_start
            v_loss /= self.t - self.t_start

        if self.process_idx == 0:
            logger.debug('pi_loss:%s v_loss:%s', pi_loss.data, v_loss.data)

        total_loss = pi_loss + F.reshape(v_loss, pi_loss.data.shape)

        # Compute gradients using thread-specific model
        self.model.zerograds()
        total_loss.backward()
        # Copy the gradients to the globally shared model
        self.shared_model.zerograds()
        copy_param.copy_grad(
            target_link=self.shared_model, source_link=self.model)
        # Update the globally shared model
        if self.process_idx == 0:
            norm = self.optimizer.compute_grads_norm()
            logger.debug('grad norm:%s', norm)
        self.optimizer.update()
        if self.process_idx == 0:
            logger.debug('update')

        self.sync_parameters()
        if isinstance(self.model, Recurrent):
            self.model.unchain_backward()

        self.past_action_log_prob = {}
        self.past_action_entropy = {}
        self.past_states = {}
        self.past_rewards = {}
        self.past_values = {}
        self.past_action_distrib = {}
        self.past_avg_action_distrib = {}

        self.t_start = self.t

    def act_and_train(self, state, reward):

        statevar = np.expand_dims(self.phi(state), 0)

        self.past_rewards[self.t - 1] = reward

        if self.t - self.t_start == self.t_max:
            self.update_on_policy(statevar)
            for _ in range(self.n_times_replay):
                self.update_from_replay()

        self.past_states[self.t] = statevar
        action_distrib, action_value = self.model(statevar)
        action = action_distrib.sample()
        action.creator = None  # Do not backprop through sampled actions

        # Save values for a later update
        self.past_action_log_prob[self.t] = action_distrib.log_prob(action)
        self.past_action_entropy[self.t] = action_distrib.entropy
        v = F.sum(action_distrib.all_prob * action_value.q_values, axis=1)
        self.past_values[self.t] = v
        self.past_action_distrib[self.t] = action_distrib
        with chainer.no_backprop_mode():
            avg_action_distrib, _ = self.shared_average_model(
                statevar)
        self.past_avg_action_distrib[self.t] = avg_action_distrib

        self.t += 1
        action = action.data[0]
        if self.process_idx == 0:
            logger.debug('t:%s r:%s a:%s action_distrib:%s',
                         self.t, reward, action, action_distrib)
        # Update stats
        self.average_value += (
            (1 - self.average_value_decay) *
            (float(v.data[0]) - self.average_value))
        self.average_entropy += (
            (1 - self.average_entropy_decay) *
            (float(action_distrib.entropy.data[0]) - self.average_entropy))

        if self.last_state is not None:
            assert self.last_action is not None
            assert self.last_action_distrib is not None
            # Add a transition to the replay buffer
            self.replay_buffer.append(
                state=self.last_state,
                action=self.last_action,
                reward=reward,
                next_state=state,
                next_action=action,
                is_state_terminal=False,
                mu=self.last_action_distrib,
            )

        self.last_state = state
        self.last_action = action
        self.last_action_distrib = copy.copy(action_distrib)
        self.last_action_distrib.creator = None

        return action

    def act(self, obs):
        # Use the process-local model for acting
        with chainer.no_backprop_mode():
            statevar = np.expand_dims(self.phi(obs), 0)
            action_distrib, _ = self.model(statevar)
            if self.act_deterministically:
                return action_distrib.most_probable.data[0]
            else:
                return action_distrib.sample().data[0]

    def stop_episode_and_train(self, state, reward, done=False):
        assert self.last_state is not None
        assert self.last_action is not None

        self.past_rewards[self.t - 1] = reward
        if done:
            self.update_on_policy(None)
        else:
            statevar = np.expand_dims(self.phi(state), 0)
            self.update_on_policy(statevar)
        for _ in range(self.n_times_replay):
            self.update_from_replay()

        if isinstance(self.model, Recurrent):
            self.model.reset_state()
            self.shared_average_model.reset_state()

        # Add a transition to the replay buffer
        self.replay_buffer.append(
            state=self.last_state,
            action=self.last_action,
            reward=reward,
            next_state=state,
            next_action=self.last_action,
            is_state_terminal=done,
            mu=self.last_action_distrib)
        self.replay_buffer.stop_current_episode()

        self.last_state = None
        self.last_action = None
        self.last_action_distrib = None

    def stop_episode(self):
        if isinstance(self.model, Recurrent):
            self.model.reset_state()
            self.shared_average_model.reset_state()

    def save(self, dirname):
        makedirs(dirname, exist_ok=True)
        # Save the process-local model
        serializers.save_npz(os.path.join(dirname, 'model.npz'), self.model)
        serializers.save_npz(
            os.path.join(dirname, 'optimizer.npz'), self.optimizer)

    def load(self, dirname):
        serializers.load_npz(os.path.join(dirname, 'model.npz'), self.model)
        copy_param.copy_param(target_link=self.shared_model,
                              source_link=self.model)

        opt_filename = os.path.join(dirname, 'optimizer.npz')
        if os.path.exists(opt_filename):
            serializers.load_npz(opt_filename, self.optimizer)
        else:
            print('WARNING: {0} was not found, so loaded only a model'.format(
                opt_filename))

    def get_stats_keys(self):
        return ('average_value', 'average_entropy')

    def get_stats_values(self):
        return (self.average_value, self.average_entropy)
