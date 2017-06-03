from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

try:
    import better_exceptions
except ImportError:
    pass

from six.moves import xrange

from util import log
from pprint import pprint

import tensorflow.contrib.slim as slim

from input_ops import create_input_ops

import os
import time
import numpy as np
import tensorflow as tf

class Trainer(object):

    @staticmethod
    def get_model_class(model_name):
        if model_name == 'MLP':
            from model import Model
        else:
            raise ValueError(model_name)
        return Model

    def __init__(self,
                 config,
                 dataset,
                 dataset_test):
        self.config = config
        hyper_parameter_str = config.dataset+'_lr_'+str(config.learning_rate)
        self.train_dir = './train_dir/%s-%s-%s-%s' % (
            config.model,
            config.prefix,
            hyper_parameter_str,
            time.strftime("%Y%m%d-%H%M%S")
        )

        if not os.path.exists(self.train_dir): os.makedirs(self.train_dir)
        log.infov("Train Dir: %s", self.train_dir)

        # --- input ops ---
        self.batch_size = config.batch_size

        _, self.batch_train = create_input_ops(dataset, self.batch_size,
                                               is_training=True)
        _, self.batch_test = create_input_ops(dataset_test, self.batch_size,
                                              is_training=False)

        # --- create model ---
        Model = self.get_model_class(config.model)
        log.infov("Using Model class : %s", Model)
        self.model = Model(config)

        # --- optimizer ---
        self.global_step = tf.contrib.framework.get_or_create_global_step(graph=None)
        self.learning_rate = config.learning_rate
        self.learning_rate = tf.train.exponential_decay(
            self.learning_rate,
            global_step=self.global_step,
            decay_steps=5000,
            decay_rate=0.5,
            staircase=True,
            name='decaying_learning_rate'
        )
        # print all the trainable variables
        #tf.contrib.slim.model_analyzer.analyze_vars(tf.trainable_variables(), print_info=True)

        #self.check_op = tf.add_check_numerics_ops()
        self.check_op = tf.no_op()

        # --- checkpoint and monitoring ---
        all_vars = tf.trainable_variables()

        d_var = [v for v in all_vars if v.name.startswith('Discriminator')]
        log.warn("********* d_var ********** "); slim.model_analyzer.analyze_vars(d_var, print_info=True)

        g_var = [v for v in all_vars if v.name.startswith(('Generator'))]
        log.warn("********* g_var ********** "); slim.model_analyzer.analyze_vars(g_var, print_info=True)

        rem_var = (set(all_vars) - set(d_var) - set(g_var))
        print([v.name for v in rem_var]); assert not rem_var

        self.d_optimizer = tf.contrib.layers.optimize_loss(
            loss=self.model.d_loss,
            global_step=self.global_step,
            learning_rate=self.learning_rate,
            optimizer=tf.train.AdamOptimizer,
            clip_gradients=20.0,
            name='d_optimize_loss',
            variables=d_var
        )

        self.g_optimizer = tf.contrib.layers.optimize_loss(
            loss=self.model.g_loss,
            global_step=self.global_step,
            learning_rate=self.learning_rate,
            optimizer=tf.train.AdamOptimizer,
            clip_gradients=20.0,
            name='g_optimize_loss',
            variables=g_var
        )

        self.summary_op = tf.summary.merge_all()

        self.saver = tf.train.Saver(max_to_keep=100)
        self.summary_writer = tf.summary.FileWriter(self.train_dir)

        self.checkpoint_secs = 600  # 10 min

        self.supervisor =  tf.train.Supervisor(
            logdir=self.train_dir,
            is_chief=True,
            # saver=self.saver,
            saver=None,
            summary_op=None,
            summary_writer=self.summary_writer,
            save_summaries_secs=300,
            save_model_secs=self.checkpoint_secs,
            global_step=self.global_step,
        )

        session_config = tf.ConfigProto(
            allow_soft_placement=True,
            gpu_options=tf.GPUOptions(allow_growth=True),
            device_count={'GPU': 1},
        )
        self.session = self.supervisor.prepare_or_wait_for_session(config=session_config)

        self.ckpt_path = config.checkpoint
        if self.ckpt_path is not None:
            log.info("Checkpoint path: %s", self.ckpt_path)
            self.pretrain_saver.restore(self.session, self.ckpt_path)
            log.info("Loaded the pretrain parameters from the provided checkpoint path")

    def train(self):
        log.infov("Training Starts!")
        pprint(self.batch_train)

        max_steps = 1000000

        output_save_step = 1000
        test_sample_step = 500

        for s in xrange(max_steps):
            step, loss_train, summary, d_loss, g_loss, s_loss, step_time, prediction_train, gt_train = \
                self.run_single_step(self.batch_train, step=s, is_train=True)

            # periodic inference
            if s % test_sample_step == 0:
                loss_test, prediction_test, gt_test = \
                    self.run_test(self.batch_test, is_train=False)
            else:
                loss_test = 0.0

            if s % 10 == 0:
                self.log_step_message(step, loss_train, loss_test, d_loss, g_loss, s_loss, step_time)


            self.summary_writer.add_summary(summary, global_step=step)

            if s % output_save_step == 0:
                log.infov("Saved checkpoint at %d", s)
                save_path = self.saver.save(self.session, os.path.join(self.train_dir, 'model'), global_step=step)

    def run_single_step(self, batch, step=None, is_train=True):
        _start_time = time.time()

        batch_chunk = self.session.run(batch)

        if step%(self.config.update_rate+1) > 0:
        # Train the generator
            fetch = [self.global_step, self.model.total_loss, self.summary_op, self.model.d_loss, self.model.g_loss,
                     self.model.S_loss, self.model.all_preds, self.model.all_targets, self.g_optimizer, self.check_op]
        elif step%(self.config.update_rate+1) ==  0:
        # Train the discriminator
            fetch = [self.global_step, self.model.total_loss, self.summary_op, self.model.d_loss, self.model.g_loss,
                     self.model.S_loss, self.model.all_preds, self.model.all_targets, self.d_optimizer, self.check_op]

        fetch_values = self.session.run(fetch,
            feed_dict=self.model.get_feed_dict(batch_chunk, step=step)
        )
        [step, loss, summary, d_loss, g_loss, s_loss, all_preds, all_targets] = fetch_values[:8]

        _end_time = time.time()

        return step, loss, summary, d_loss, g_loss, s_loss,  (_end_time - _start_time), all_preds, all_targets

    def run_test(self, batch, is_train=False, repeat_times=8):

        batch_chunk = self.session.run(batch)

        # run multiple times due to randomness of z
        gt = None
        pred_list = []
        for rep in range(repeat_times):
            [step, loss, all_preds, all_targets] = self.session.run(
                [self.global_step, self.model.total_loss, self.model.all_preds, self.model.all_targets],
                feed_dict=self.model.get_feed_dict(batch_chunk, is_training=False)
            )
            gt = all_targets[0]  # just first item in the batch
            pred_list.append(all_preds[0])  # just first item in the batch

        return loss, all_preds, all_targets

    def log_step_message(self, step, loss_train, loss_test, d_loss, g_loss, s_loss, step_time, is_train=True):
        if step_time == 0: step_time = 0.001
        log_fn = (is_train and log.info or log.infov)
        log_fn((" [{split_mode:5s} step {step:4d}] " +
                "total loss: {train_loss:.5f} " +
                "S loss: {s_loss:.5f} " +
                "D loss: {d_loss:.5f} " +
                "G loss: {g_loss:.5f} " +
                "test loss: {test_loss:.5f} " +
                "({sec_per_batch:.3f} sec/batch, {instance_per_sec:.3f} instances/sec) "
                ).format(split_mode=(is_train and 'train' or 'val'),
                         step = step,
                         d_loss = d_loss,
                         g_loss = g_loss,
                         s_loss = s_loss,
                         train_loss = loss_train,
                         test_loss = loss_test,
                         sec_per_batch = step_time,
                         instance_per_sec = self.batch_size / step_time,
                         )
               )

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch_size', type=int, default=8)
    parser.add_argument('--model', type=str, default='MLP')
    parser.add_argument('--prefix', type=str, default='default')
    parser.add_argument('--checkpoint', type=str, default=None)
    parser.add_argument('--dataset', type=str, default='mnist', choices=['mnist'])
    parser.add_argument('--learning_rate', type=float, default=1e-3)
    parser.add_argument('--update_rate', type=int, default=1)
    """
    parser.add_argument('--input_height', type=int, default=28)
    parser.add_argument('--input_width', type=int, default=28)
    parser.add_argument('--num_class', type=int, default=10)
    """
    config = parser.parse_args()

    if config.dataset == 'mnist':
        from mnist_dataset import create_default_splits
        config.input_height = 28
        config.input_width = 28
        config.num_class = 10
        dataset_train, dataset_test = create_default_splits()
    else:
        raise ValueError(config.dataset)

    trainer = Trainer(config,
                      dataset_train, dataset_test)

    log.warning("dataset: %s, learning_rate: %f", config.dataset, config.learning_rate)
    trainer.train()

if __name__ == '__main__':
    main()