""" Main """

import argparse
import json
import os
import threading

import tensorflow as tf

from datasets import dataset_factory
from models import autoconverter

slim = tf.contrib.slim


class EvaluationRunHook(tf.train.SessionRunHook):
    def __init__(self,
                 checkpoint_dir,
                 graph,
                 eval_frequency,
                 eval_steps=None,
                 **kwargs):
        self._eval_steps = eval_steps
        self._checkpoint_dir = checkpoint_dir
        self._kwargs = kwargs
        self._eval_every = eval_frequency
        self._latest_checkpoint = None
        self._checkpoints_since_eval = 0
        self._graph = graph

        with graph.as_default():
            # Op that creates a Summary protocol buffer by merging summaries
            self._summary_op = tf.summary.merge_all()
            # Saver class add ops to save and restore
            # variables to and from checkpoint
            self._saver = tf.train.Saver()
            # Creates a global step to contain a counter for
            # the global training step
            self._gs = tf.train.get_or_create_global_step()

        # MonitoredTrainingSession runs hooks in background threads
        # and it doesn't wait for the thread from the last session.run()
        # call to terminate to invoke the next hook, hence locks.
        self._eval_lock = threading.Lock()
        self._checkpoint_lock = threading.Lock()
        self._file_writer = tf.summary.FileWriter(
            os.path.join(checkpoint_dir, 'eval'), graph=graph)

    def after_run(self, run_context, run_values):
        # Always check for new checkpoints in case a single evaluation
        # takes longer than checkpoint frequency and _eval_every is >1
        self._update_latest_checkpoint()

        if self._eval_lock.acquire(False):
            try:
                if self._checkpoints_since_eval > self._eval_every:
                    self._checkpoints_since_eval = 0
                    self._run_eval()
            finally:
                self._eval_lock.release()

    def _update_latest_checkpoint(self):
        """Update the latest checkpoint file created in the output dir."""
        if self._checkpoint_lock.acquire(False):
            try:
                latest = tf.train.latest_checkpoint(self._checkpoint_dir)
                if not latest == self._latest_checkpoint:
                    self._checkpoints_since_eval += 1
                    self._latest_checkpoint = latest
            finally:
                self._checkpoint_lock.release()

    def end(self, session):
        """Called at then end of session to make sure we always evaluate."""
        self._update_latest_checkpoint()

        with self._eval_lock:
            self._run_eval()

    def _run_eval(self):
        """Run model evaluation and generate summaries."""
        coord = tf.train.Coordinator(clean_stop_exception_types=(
            tf.errors.CancelledError, tf.errors.OutOfRangeError))

        with tf.Session(graph=self._graph) as session:
            # Restores previously saved variables from latest checkpoint
            self._saver.restore(session, self._latest_checkpoint)

            session.run([
                tf.tables_initializer(),
                tf.local_variables_initializer()
            ])
            tf.train.start_queue_runners(coord=coord, sess=session)
            train_step = session.run(self._gs)

            tf.logging.info('Starting Evaluation For Step: {}'.format(train_step))
            with coord.stop_on_exception():
                eval_step = 0
                while not coord.should_stop() and (self._eval_steps is None or
                                                           eval_step < self._eval_steps):
                    summaries = session.run(
                        self._summary_op)
                    if eval_step % 100 == 0:
                        tf.logging.info("On Evaluation Step: {}".format(eval_step))
                    eval_step += 1

            # Write the summaries
            self._file_writer.add_summary(summaries, global_step=train_step)
            self._file_writer.flush()


def run(target,
        is_chief,
        train_steps,
        eval_steps,
        job_dir,
        learning_rate,
        eval_frequency,
        dataset_name,
        domain_a,
        domain_b,
        dataset_dir,
        train_batch_size,
        eval_batch_size):
    ######################
    # Select the dataset #
    ######################
    dataset_a = dataset_factory.get_dataset(
        dataset_name, domain_a, dataset_dir)
    dataset_b = dataset_factory.get_dataset(
        dataset_name, domain_b, dataset_dir)

    # If the server is chief which is `master`
    # In between graph replication Chief is one node in
    # the cluster with extra responsibility and by default
    # is worker task zero. We have assigned master as the chief.
    #
    # See https://youtu.be/la_M6bCV91M?t=1203 for details on
    # distributed TensorFlow and motivation about chief.
    if is_chief:
        # Do evaluation job
        evaluation_graph = tf.Graph()
        with evaluation_graph.as_default():
            # Inputs
            images_a, images_b = autoconverter.input_fn(
                dataset_a, dataset_b,
                batch_size=eval_batch_size, is_training=False)

            # Model
            outputs = autoconverter.model_fn(
                images_a, images_b, learning_rate, is_training=False)

        hooks = [EvaluationRunHook(
            job_dir,
            evaluation_graph,
            eval_frequency,
            eval_steps=eval_steps,
        )]
    else:
        hooks = []

    with tf.Graph().as_default():
        # Placement of ops on devices using replica device setter
        # which automatically places the parameters on the `ps` server
        # and the `ops` on the workers
        #
        # See:
        # https://www.tensorflow.org/api_docs/python/tf/train/replica_device_setter
        with tf.device(tf.train.replica_device_setter()):
            # Inputs
            images_a, images_b = autoconverter.input_fn(
                dataset_a, dataset_b,
                batch_size=train_batch_size, is_training=True)

            # Model
            train_op, global_step, outputs = autoconverter.model_fn(
                images_a, images_b, learning_rate, is_training=True)

        # Creates a MonitoredSession for training
        # MonitoredSession is a Session-like object that handles
        # initialization, recovery and hooks
        # https://www.tensorflow.org/api_docs/python/tf/train/MonitoredTrainingSession
        with tf.train.MonitoredTrainingSession(master=target,
                                               is_chief=is_chief,
                                               checkpoint_dir=job_dir,
                                               hooks=hooks,
                                               save_checkpoint_secs=300,
                                               save_summaries_steps=50) as session:
            # Global step to keep track of global number of steps particularly in
            # distributed setting
            step = global_step.eval(session=session)

            # Run the training graph which returns the step number as tracked by
            # the global step tensor.
            # When train epochs is reached, session.should_stop() will be true.
            while (train_steps is None or step < train_steps) \
                    and not session.should_stop():
                step, _ = session.run([global_step, train_op])

        # TODO: Export model


def dispatch(*args, **kwargs):
    """Parse TF_CONFIG to cluster_spec and call run() method
      TF_CONFIG environment variable is available when running using
      gcloud either locally or on cloud. It has all the information required
      to create a ClusterSpec which is important for running distributed code.
    """

    tf_config = os.environ.get('TF_CONFIG')

    # If TF_CONFIG is not available run local
    if not tf_config:
        return run('', True, *args, **kwargs)

    tf_config_json = json.loads(tf_config)

    cluster = tf_config_json.get('cluster')
    job_name = tf_config_json.get('task', {}).get('type')
    task_index = tf_config_json.get('task', {}).get('index')

    # If cluster information is empty run local
    if job_name is None or task_index is None:
        return run('', True, *args, **kwargs)

    cluster_spec = tf.train.ClusterSpec(cluster)
    server = tf.train.Server(cluster_spec,
                             job_name=job_name,
                             task_index=task_index)

    # Wait for incoming connections forever
    # Worker ships the graph to the ps server
    # The ps server manages the parameters of the model.
    #
    # See a detailed video on distributed TensorFlow
    # https://www.youtube.com/watch?v=la_M6bCV91M
    if job_name == 'ps':
        server.join()
        return
    elif job_name in ['master', 'worker']:
        return run(server.target, job_name == 'master', *args, **kwargs)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    parser.add_argument('--train-steps',
                        type=int,
                        help='Maximum number of training steps to perform.')
    parser.add_argument('--eval-steps',
                        help="""\
                          Number of steps to run evalution for at each checkpoint.
                          If unspecified, will run for 1 full epoch over training
                          data""",
                        default=None,
                        type=int)
    parser.add_argument('--eval-frequency',
                        default=1,
                        help='Perform one evaluation per n steps')
    parser.add_argument('--job-dir',
                        required=True,
                        type=str,
                        help="""\
                          GCS or local dir for checkpoints, exports, and
                          summaries. Use an existing directory to load a
                          trained model, or a new directory to retrain""")
    parser.add_argument('--dataset-name',
                        type=str,
                        help='The name of the dataset to load.',
                        choices=dataset_factory.datasets_map.keys(),
                        default='celeba')
    parser.add_argument('--domain-a',
                        type=str,
                        help='The name of the domain A.',
                        default='black_hair')
    parser.add_argument('--domain-b',
                        type=str,
                        help='The name of the domain B.',
                        default='blond_hair')
    parser.add_argument('--dataset-dir',
                        required=True,
                        type=str,
                        help='The directory where the dataset files are stored.')
    parser.add_argument('--train-batch-size',
                        type=int,
                        default=40,
                        help='Batch size for training steps')
    parser.add_argument('--eval-batch-size',
                        type=int,
                        default=40,
                        help='Batch size for evaluation steps')
    parser.add_argument('--learning-rate',
                        type=float,
                        default=0.002,
                        help='Learning rate for SGD')

    parser.add_argument('--verbosity',
                        choices=[
                            'DEBUG',
                            'ERROR',
                            'FATAL',
                            'INFO',
                            'WARN'
                        ],
                        default='INFO',
                        help='Set logging verbosity')

    parse_args, unknown = parser.parse_known_args()

    # Set python level verbosity
    tf.logging.set_verbosity(parse_args.verbosity)
    # Set C++ Graph Execution level verbosity
    os.environ['TF_CPP_MIN_LOG_LEVEL'] = str(
        tf.logging.__dict__[parse_args.verbosity] / 10)
    del parse_args.verbosity

    if unknown:
        tf.logging.warn('Unknown arguments: {}'.format(unknown))

    dispatch(**parse_args.__dict__)
