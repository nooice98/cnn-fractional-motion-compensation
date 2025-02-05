# BSD 3-Clause License
#
# Copyright (c) 2021 BBC
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#  1. Redistributions of source code must retain the above copyright notice,
#  this list of conditions and the following disclaimer.
#
#  2. Redistributions in binary form must reproduce the above copyright notice,
#  this list of conditions and the following disclaimer in the documentation
#  and/or other materials provided with the distribution.
#
#  3. Neither the name of the copyright holder nor the names of its contributors may
#  be used to endorse or promote products derived from this software without
#  specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS
# BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF
# THE POSSIBILITY OF SUCH DAMAGE.

from utils import read_shared_data, read_combined_data, read_combined_testdata, \
    calculate_batch_number, calculate_test_error, save_results
from model_base import BaseCNN
import time
import os
import tensorflow as tf
import numpy as np
import math


class CompetitionBaseCNN(BaseCNN):
    """
    Class for the base CNN for shared models that contains generalised parameters and functions
    """
    def __init__(self, sess, cfg):
        super().__init__(sess, cfg)

        self.vvc_loss = tf.placeholder(tf.float32, [None], name='vvc_loss')
        self.epoch = tf.placeholder(tf.int32, name='epoch')
        self.subset = tf.placeholder(tf.int32, name='subset')
        self.batch_size = tf.placeholder(tf.int32, name='batch_size')

    def train(self):
        """
        Training procedure for the CNN model: read dataset, initialize graph, load model checkpoint if possible,
                                              train and validate model on different block sizes,
                                              save model if it performs better than in the previous epoch,
                                              stop training after all epochs or if model doesn't improve for x epochs
        """
        # get training and validation inputs/labels/SAD losses
        train_data_sub, train_label_sub, train_sad_sub, val_data_sub, val_label_sub, val_sad_sub = \
            read_shared_data(self.cfg.dataset_dir, self.cfg.batch_size)
        train_data, train_label, train_sad, val_data, val_label, val_sad = \
            read_combined_data(train_data_sub, train_label_sub, train_sad_sub, val_data_sub, val_label_sub, val_sad_sub)

        # initialize logging
        writer, merged = self.initialize_graph(self.subdirectory())

        # load model if possible
        global_step = self.load(self.subdirectory())

        # calculate number of training / validation batches for each block size per fractional position
        batch_train, batch_val = calculate_batch_number(train_data_sub, val_data_sub, self.cfg.batch_size, nested=True)

        start_epoch = global_step // sum([x*15 for x in batch_train])
        print("Training %s network, from epoch %d" % (self.cfg.model_name.upper(), start_epoch))

        start_time = time.time()
        err_train = None
        for ep in range(start_epoch, self.cfg.epoch):
            # Training for first epoch where all branches are updated
            # and for the latter epochs where only the best (and better than VVC) branch is updated
            if ep != 1:
                # Run on batches of combined training inputs
                for index, block in enumerate(train_data):
                    for idx in range(batch_train[index] * 15):
                        feed_dict = self.competition_feed_dict(train_data[block], train_label[block], train_sad[block],
                                                               idx, ep, 0)
                        # Run validation to avoid possible issue in latter epochs
                        # where none of the branches are better than VVC resulting in a nan error
                        err_valid = self.sess.run([self.loss], feed_dict=feed_dict)
                        if np.isnan(err_valid[0]):
                            continue
                        else:
                            _, err_train, summary = self.sess.run([self.train_op, self.loss, merged],
                                                                  feed_dict=feed_dict)
                            global_step += 1
                            writer.add_summary(summary, global_step)

                # Run on batches of combined validation inputs
                error_val_list = []
                for index, block in enumerate(val_data):
                    for idx in range(batch_val[index]):
                        feed_dict = self.competition_feed_dict(val_data[block], val_label[block], val_sad[block],
                                                               idx, ep, 0)
                        err_valid = self.sess.run([self.loss], feed_dict=feed_dict)
                        if np.isnan(err_valid[0]):
                            continue
                        error_val_list.append(err_valid[0])

                err_val = sum(error_val_list) / len(error_val_list)
            else:
                # First epoch where each branch is trained for a particular data subset
                # Run on batches of training inputs, per fractional position and block size
                for index, block in enumerate(train_data_sub):
                    for idx in range(batch_train[index]):
                        for i, frac in enumerate(train_data_sub[block]):
                            feed_dict = self.competition_feed_dict(train_data_sub[block][frac],
                                                                   train_label_sub[block][frac],
                                                                   train_sad_sub[block][frac], idx, ep, i)
                            _, err_train, summary = self.sess.run([self.train_op, self.loss, merged],
                                                                  feed_dict=feed_dict)
                            global_step += 1
                            writer.add_summary(summary, global_step)

                # Run on batches of validation inputs, per fractional position and block size
                error_val_list = []
                for index, block in enumerate(val_data_sub):
                    for idx in range(batch_val[index]):
                        for i, frac in enumerate(val_data_sub[block]):
                            feed_dict = self.competition_feed_dict(val_data_sub[block][frac],
                                                                   val_label_sub[block][frac],
                                                                   val_sad_sub[block][frac], idx, ep, i)
                            err_valid = self.sess.run([self.loss], feed_dict=feed_dict)
                            error_val_list.append(err_valid[0])

                err_val = sum(error_val_list) / len(error_val_list)

            # save model if better than previously, check early stopping condition
            counter = self.save_epoch(ep, global_step, start_time, err_train, err_val, self.subdirectory())
            if counter == self.cfg.early_stopping - 1:
                break

    def test(self):
        """
        Testing procedure for the CNN model: read dataset, initialize variables, load model checkpoint,
                                              test model on different block sizes,
                                              calculate SAD loss and compare to VVC,
                                              save results to specified directory
        """
        test_data, test_label, test_sad = read_combined_testdata(self.cfg.test_dataset_dir)

        tf.global_variables_initializer().run()

        # load model
        global_step = self.load(self.subdirectory())
        if not global_step:
            raise SystemError("Failed to load a trained model!")

        print("Testing %s network" % self.cfg.model_name.upper())

        # Run test, per block size
        error_pred, error_vvc, error_blocks = ([] for _ in range(3))
        for block in test_data:
            batch_test = math.ceil(len(test_data[block]) / self.cfg.batch_size)
            result = np.array([])

            for idx in range(batch_test):
                feed_dict = self.competition_feed_dict(test_data[block], test_label[block], test_sad[block], idx, 2, 0)
                res = self.sess.run([self.pred], feed_dict=feed_dict)
                cropped_input = feed_dict[self.inputs][:, self.half_kernel:-self.half_kernel,
                                                       self.half_kernel:-self.half_kernel, :]
                result = np.vstack([result, res[0] + cropped_input]) if result.size else res[0] + cropped_input

            # calculate SAD NN loss and compare it to VVC loss
            nn_cost, vvc_cost, switch_cost = calculate_test_error(result, test_label[block], test_sad[block])
            error_pred.append(nn_cost)
            error_vvc.append(vvc_cost)
            error_blocks.append(switch_cost)

        save_results(self.cfg.results_dir, self.cfg.model_name, self.subdirectory(),
                     error_pred, error_vvc, error_blocks)

    def subdirectory(self):
        """
        Model subdirectory details
        """
        return os.path.join(self.cfg.model_name, self.cfg.dataset_dir.split("/")[1])

    def competition_feed_dict(self, inputs, labels, sad, i, epoch, subset):
        """
        Method that prepares a batch of inputs / labels to be fed into the competition model
        :param inputs: input data
        :param labels: label data
        :param sad: SAD loss data
        :param i: index pointing to the current position within the data
        :param epoch: current epoch number, needed for choosing the training method in the framework
        :param subset: index indicating which branch of the output layer to update
        :return a batch-sized dictionary of inputs / labels / subset / batch_size
        """
        batch_sad = sad[i * self.cfg.batch_size: (i + 1) * self.cfg.batch_size]
        feed_dict = self.prepare_feed_dict(inputs, labels, i)
        feed_dict.update({self.vvc_loss: batch_sad, self.epoch: epoch,
                          self.subset: subset, self.batch_size: len(feed_dict[self.inputs])})
        return feed_dict


class CompetitionCNN(CompetitionBaseCNN):
    """
    Class CompetitionCNN, a 3-layer model with 15 outputs,
    uses a 3-stage training framework:
        update all branches in first epoch,
        update specific branch in second epoch,
        update branch which minimizes the loss and is better than VVC in subsequent epochs;
    uses gradient clipping by norm
    """
    def __init__(self, sess, cfg):
        super().__init__(sess, cfg)

        self.weights = {
            'w1': tf.get_variable('w1', shape=[9, 9, 1, 64],
                                  initializer=tf.contrib.layers.variance_scaling_initializer()),
            'w2': tf.get_variable('w2', shape=[1, 1, 64, 32],
                                  initializer=tf.contrib.layers.variance_scaling_initializer()),
            'w3': tf.get_variable('w3', shape=[5, 5, 32, 15],
                                  initializer=tf.contrib.layers.variance_scaling_initializer())
        }

        # parameter half_kernel needed for residual learning
        self.calculate_half_kernel_size()

        self.pred = self.linear_model()

        self.loss = self.calculate_loss()

        # gradient clipping by norm
        optimizer = tf.train.AdamOptimizer(self.cfg.learning_rate)
        self.gradients, variables = zip(*optimizer.compute_gradients(self.loss))
        self.gradients, _ = tf.clip_by_global_norm(self.gradients, self.cfg.gradient_clip)
        self.train_op = optimizer.apply_gradients(zip(self.gradients, variables))

        self.saver = tf.train.Saver()

    def calculate_loss(self):
        cost = self.complex_loss(self.cfg.loss, self.weights[list(self.weights.keys())[-1]].get_shape()[-1].value)

        def vvc_competition():
            # find minimum loss across branches for each block in batch
            nn_loss = tf.reduce_min(cost, axis=1)

            # stack best NN loss and VVC loss to find which one is better
            comp_loss = tf.stack([nn_loss, self.vvc_loss])
            idx = tf.math.argmax(comp_loss, 0)
            mask = tf.cast(idx, tf.bool)

            # retain only NN losses which are lower than VVC loss
            nn_loss = tf.boolean_mask(nn_loss, mask)

            return nn_loss

        # update all branches in epoch 0, update specific branch in epoch 1, update best branch(es) in other epochs
        cost = tf.cond(tf.math.greater(self.epoch, 0),
                       lambda: tf.cond(tf.math.greater(self.epoch, 1),
                                       vvc_competition,
                                       lambda: tf.slice(cost, [0, self.subset], [self.batch_size, 1])),
                       lambda: tf.reduce_mean(cost, axis=1))

        return tf.reduce_mean(cost, name="loss")
