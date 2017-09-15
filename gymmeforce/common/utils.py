import os
import random
import numpy as np
from collections import deque
import tensorflow as tf


class RingBuffer:
    '''
    Similar function of a deque, but returns a numpy array directly
    Used for building an array with <maxlen> sequential states

    Args:
        state_shape: Shape of state (tuple)
        maxlen: How many states to stack
    '''
    def __init__(self, state_shape, maxlen):
        self.state_shape = state_shape
        self.maxlen = maxlen
        self.current_idx = 0
        self.reset()

    def reset(self):
        self.data = np.zeros(((self.maxlen,) + self.state_shape))

    def append(self, data):
        self.data = np.roll(self.data, -1, axis=0)
        self.data[self.maxlen - 1] = np.squeeze(data)

    def get_data(self):
        return self.data.swapaxes(0, -1)


class ReplayBuffer:
    '''
    Memory efficient implementation of replay buffer, storing each state only once.
    Example: Typical use for atari, with each frame being a 84x84 grayscale
             image (uint8), storing 1M frames should use about 7GiB of RAM
             (8 * 64 * 64 * 1M bits)

    Args:
        maxlen: Maximum number of transitions stored
        history_length: Number of sequential states stacked when sampling
        batch_size: Mini-batch size created by sample
    '''
    def __init__(self, maxlen, history_length=1, batch_size=32):
        self.initialized = False
        self.maxlen = maxlen
        self.history_length = history_length
        self.batch_size = batch_size
        self.current_idx = 0
        self.current_len = 0

    def add(self, state, action, reward, done):
        if not self.initialized:
            self.initialized = True
            state_shape = np.squeeze(state).shape
            # Allocate memory
            self.states = np.empty((self.maxlen,) + state_shape,
                                   dtype=state.dtype)
            self.actions = np.empty(self.maxlen, dtype=np.int32)
            self.rewards = np.empty(self.maxlen, dtype=np.float32)
            self.dones = np.empty(self.maxlen, dtype=np.bool)

        # Store transition
        self.states[self.current_idx] = np.squeeze(state)
        self.actions[self.current_idx] = action
        self.rewards[self.current_idx] = reward
        self.dones[self.current_idx] = done

        # Update current position
        self.current_idx = (self.current_idx + 1) % self.maxlen
        self.current_len = min(self.current_len + 1, self.maxlen)

    def sample(self):
        start_idxs, end_idxs = self._generate_idxs()
        # Get states
        b_states_t = np.array([self.states[start_idx:end_idx] for
                              start_idx, end_idx in zip(start_idxs, end_idxs)],
                              copy=False)
        b_states_tp1 = np.array([self.states[start_idx + 1: end_idx + 1] for
                                start_idx, end_idx in zip(start_idxs, end_idxs)],
                                copy=False)
        # Remember that when slicing the end_idx is not included
        actions = self.actions[end_idxs - 1]
        rewards = self.rewards[end_idxs - 1]
        dones = self.dones[end_idxs - 1]

        return (b_states_t.swapaxes(1, -1),
                b_states_tp1.swapaxes(1, -1),
                actions, rewards, dones)

    def _generate_idxs(self):
        start_idxs = []
        end_idxs = []
        while len(start_idxs) < self.batch_size:
            start_idx = np.random.randint(0, self.current_len - self.history_length)
            end_idx = start_idx + self.history_length

            # Check if idx was already picked
            if start_idx in start_idxs:
                continue
            # Only the last frame can have done == True
            if np.any(self.dones[start_idx: end_idx - 1]):
                continue

            # Valid idx!!
            start_idxs.append(start_idx)
            end_idxs.append(end_idx)

        return np.array(start_idxs), np.array(end_idxs)


def load_q_func(sess, log_dir):
    ''' Returns a function that computes the q_values '''
    # Import model from metagraph
    model_path = tf.train.latest_checkpoint(log_dir)
    print('Loading model from: {}'.format(model_path))
    saver = tf.train.import_meta_graph(model_path + '.meta')
    saver.restore(sess, model_path)

    # Fetch tensors
    q_values_tensor = tf.get_collection('q_online_t')[0]
    state_input_ph = tf.get_collection('state_input')[0]

    def compute_q_values(state):
        q_values = sess.run(q_values_tensor,
                            feed_dict={state_input_ph: state})
        return q_values

    return compute_q_values


def huber_loss(y_true, y_pred, delta=1.):
    '''
    Hubber loss is less sensitive to outliers
    https://en.wikipedia.org/wiki/Huber_loss
    '''
    error = y_true - y_pred
    condition = tf.abs(error) <= delta
    squared_error = 0.5 * tf.square(error)
    linear_error = delta * (tf.abs(error) - 0.5 * delta)
    return tf.where(condition, squared_error, linear_error)


def exponential_decay(epsilon_final, stop_exploration):
    ''' Calculate epsilon based on an exponential interpolation '''
    epsilon_step = - np.log(epsilon_final) / stop_exploration

    def get_epsilon(step):
        if step <= stop_exploration:
            return np.exp(-epsilon_step * step)
        else:
            return epsilon_final

    return get_epsilon


def linear_decay(epsilon_final, stop_exploration, epsilon_start=1):
    ''' Calculates epsilon based on a linear interpolation '''
    epsilon_step = - (epsilon_start - epsilon_final) / stop_exploration
    epsilon_steps = []

    def get_epsilon(step):
        if step <= stop_exploration:
            return epsilon_step * step + epsilon_start
        else:
            return epsilon_final

    return get_epsilon


def piecewise_linear_decay(boundaries, values, initial_value=1):
    ''' Linear interpolates between boundaries '''
    boundaries = [0] + boundaries
    final_epsilons = [initial_value * value for value in values]
    final_epsilons = [initial_value] + final_epsilons

    decay_steps = [end_step - start_step for start_step, end_step
                   in zip(boundaries[:-1], boundaries[1:])]

    decay_rates = [- (start_e - final_e) / decay_step
                   for start_e, final_e, decay_step
                   in zip(final_epsilons[:-1], final_epsilons[1:], decay_steps)]

    def get_epsilon(x):
        for boundary, x0, m, y0 in zip(boundaries[1:], boundaries[:-1], decay_rates, final_epsilons):
            if x <= boundary:
                return m * (x - x0) + y0

        # Outside of boundary
        return final_epsilons[-1]

    return get_epsilon


def egreedy_police(Q_values, epsilon):
    ''' Choose an action based on a egreedy police '''
    if np.random.random() <= epsilon:
        num_actions = len(np.squeeze(Q_values))
        return np.random.choice(np.arange(num_actions))
    else:
        return np.argmax(np.squeeze(Q_values))


def discounted_sum_rewards(rewards, gamma=0.99):
    reward_sum = 0
    discounted_rewards = []

    for reward in reversed(rewards):
        reward_sum = reward + gamma * reward_sum
        discounted_rewards.append(reward_sum)

    return discounted_rewards[::-1]