import numpy as np

from gymmeforce.agents import ReplayAgent
from gymmeforce.common.gym_utils import EpisodeRunner
from gymmeforce.common.utils import calculate_n_step_return
from gymmeforce.models import DQNModel


class DQNAgent(ReplayAgent):
    def __init__(self, env_name, target_update_freq, **kwargs):
        super().__init__(env_name, **kwargs)
        self._create_model(**kwargs)
        self.target_update_freq = target_update_freq

    def _create_model(self, **kwargs):
        self.model = DQNModel(self.env_config, **kwargs)

    def _calculate_n_step_return(self, batch):
        batch['rewards'], batch['dones'] = zip(*[
            calculate_n_step_return(r, d)
            for r, d in zip(batch['rewards'], batch['dones'])
        ])

    def _calculate_learning_rate(self):
        # Calculate learning rate
        if callable(self.learning_rate):
            lr = self.learning_rate(self.i_step)
        else:
            lr = self.learning_rate

        return lr

    def _get_batch(self):
        if self.randomize_n_step == True:
            random_n_step = np.random.randint(1, self.n_step + 1)
        else:
            random_n_step = self.n_step

        batch = self.replay_buffer.sample(random_n_step)
        self._calculate_n_step_return(batch)
        batch['learning_rate'] = self._calculate_learning_rate()
        batch['n_step'] = random_n_step

        return batch

    def select_action(self, state):
        # Concatenates <history_length> states
        self.states_history.append(state)
        state_hist = self.states_history.get_data()

        # Select action based on an egreedy policy
        self.epsilon = self.exploration_schedule(self.i_step)
        if np.random.random() <= self.epsilon:
            action = np.random.choice(self.env_config['num_actions'])
        else:
            Q_values = self.model.predict(self.sess, state_hist[np.newaxis])
            action = np.argmax(Q_values)

        return action

    def train(self,
              num_steps,
              n_step,
              learning_rate,
              exploration_schedule,
              replay_buffer_size,
              randomize_n_step=False,
              learning_freq=4,
              init_buffer_size=0.05,
              batch_size=32,
              record_freq=None,
              log_steps=2e4):
        '''
        Trains the agent following these steps:
            0. Populate replay buffer (init_buffer_size) with transitions of a random agent
            1. Use the current state to calculate Q-values
               and choose an action based on an epsilon-greedy policy
            2. Store experience on the replay buffer
            3. Every <learning_freq> steps sample the buffer
               and performs gradient descent

        Args:
            num_steps: Number of steps to train the agent
            n_step: Number of steps to use reward before bootstraping
            learning_rate: Float or a function that returns a float
                           when called with the current time step as input
                           (see gymmeforce.utils.linear_decay as an example)
            exploration_schedule: Function that returns a float when
                                  called with the current time step as input
                                  (see utils.linear_decay as an example)
            replay_buffer_size: Maximum number of transitions stored on replay buffer
            target_update_freq: Number of steps between each target update
            randomize_n_step: Choose a random n_step (from 1 to n_step) each batch
            target_soft_update: Percentage of online weigth value to copy to target on
                                each update, (e.g. 1 makes target weights = online weights)
            gamma: Discount factor on sum of rewards
            grad_clip_norm: Value to clip the gradient so that its L2-norm is less than or
                            equal to grad_clip_norm
            learning_freq: Number of steps between each gradient descent update
            init_buffer_size: Percentage of buffer filled with random transitions
                              before the training starts
            batch_size: Number of samples to use when creating mini-batch from replay buffer
            record_freq: Number of episodes between each recording
            log_steps: Number of steps between each log status
        '''
        super().train()
        self.n_step = n_step
        self.randomize_n_step = randomize_n_step
        self.learning_rate = learning_rate
        self.exploration_schedule = exploration_schedule
        self.i_step = self.model.get_global_step(self.sess)
        # Create enviroment
        monitored_env, env = self._create_env('videos/train', record_freq)
        ep_runner = EpisodeRunner(env)

        self._populate_replay_buffer(ep_runner, replay_buffer_size, init_buffer_size,
                                     batch_size, n_step)

        print('Started training')
        num_episodes = 0
        reward_sum = 0
        # TODO: soft updating here, need to hard copy weights
        self.model.update_target_net(self.sess)
        while True:
            trajectory = self._play_and_add_to_buffer(ep_runner)
            reward_sum += trajectory['reward']

            if trajectory['done']:
                self.logger.add_log('Reward/Life', reward_sum)
                reward_sum = 0

            # Perform gradient descent
            if self.i_step % learning_freq == 0:
                batch = self._get_batch()
                self.model.fit(self.sess, batch)

            # Update target network
            if self.i_step % self.target_update_freq == 0:
                self.model.update_target_net(self.sess)

            # Write logs
            if self.i_step % log_steps == 0:
                self.model.increase_global_step(self.sess, log_steps)
                # Save model
                self.model.save(self.sess)
                # Calculate rewards statistics
                ep_rewards = monitored_env.get_episode_rewards()
                num_episodes_old = num_episodes
                num_episodes = len(ep_rewards)
                num_new_episodes = num_episodes - num_episodes_old
                mean_ep_rewards = np.mean(ep_rewards[-num_new_episodes:])
                # Write summaries
                self.model.write_logs(self.sess)
                # Write logs
                self.logger.add_log('Reward/Episode (unclipped)', mean_ep_rewards)
                self.logger.add_log('Learning Rate', batch['learning_rate'], precision=5)
                self.logger.add_log('Exploration Rate', self.epsilon, precision=3)
                self.logger.timeit(log_steps, max_steps=num_steps)
                self.logger.log('Step {}/{} ({:.2f}%)'.format(
                    self.i_step, int(num_steps), 100 * self.i_step / num_steps))

            self.i_step += 1
            # Check for termination
            if self.i_step >= num_steps:
                break
