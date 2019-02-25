# Copyright 2017 Google Inc. and Skytruth Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import, division
import argparse
import json
from .model import ModelBase
from . import layers
from classification import metadata
from .objectives import (
    TrainNetInfo, MultiClassificationObjective, LogRegressionObjectiveMAE)
from classification.feature_generation import vessel_feature_generation
import logging
import math
import numpy as np
import os

import tensorflow as tf


class Model(ModelBase):

    window_size = 3
    feature_depths = [48, 64, 96, 128, 192, 256, 384, 512, 768]
    strides = [2] * 9
    assert len(strides) == len(feature_depths)
    feature_sub_depths = 1024

    initial_learning_rate = 100e-5
    learning_decay_rate = 0.2
    decay_examples = 100000

    virtual_batch_size = 16

    @property
    def number_of_steps(self):
        return 700000

    @property
    def max_window_duration_seconds(self):
        return 180 * 24 * 3600

    @property
    def batch_size(self):
        return 128

    @property
    def window_max_points(self):
        nominal_max_points = (self.max_window_duration_seconds / (5 * 60)) / 4
        layer_reductions = np.prod(self.strides)
        final_size = int(round(nominal_max_points / layer_reductions))
        max_points = final_size * layer_reductions
        logging.info('Using %s points', max_points)
        return max_points

    @property
    def min_viable_timeslice_length(self):
        return 500

    def __init__(self, num_feature_dimensions, vessel_metadata, metrics):
        super(Model, self).__init__(num_feature_dimensions, vessel_metadata)

        class XOrNan:
            def __init__(self, key):
                self.key = key

            def __call__(self, mmsi):
                x = vessel_metadata.vessel_label(self.key, mmsi)
                if x == '':
                    x = np.nan
                return np.float32(x)

        self.training_objectives = [
            LogRegressionObjectiveMAE(
                'length',
                'Vessel-length',
                XOrNan('length'),
                metrics=metrics,
                loss_weight=0.1),
            LogRegressionObjectiveMAE(
                'tonnage',
                'Vessel-tonnage',
                XOrNan('tonnage'),
                metrics=metrics,
                loss_weight=0.1),
            LogRegressionObjectiveMAE(
                'engine_power',
                'Vessel-engine-Power',
                XOrNan('engine_power'),
                metrics=metrics,
                loss_weight=0.1),
            LogRegressionObjectiveMAE(
                'crew_size',
                'Vessel-Crew-Size',
                XOrNan('crew_size'),
                metrics=metrics,
                loss_weight=0.1),
            MultiClassificationObjective(
                "Multiclass", "Vessel-class", vessel_metadata, metrics=metrics, loss_weight=1)
        ]

        self.objective_map = {obj.name : obj for obj in self.training_objectives}

    def _build_net(self, features, timestamps, mmsis, is_training):
        outputs, _ = layers.misconception_model(
            features,
            filters_list=self.feature_depths,
            kernel_size=self.window_size,
            strides_list=self.strides,
            objective_functions=self.training_objectives,
            training=is_training,
            sub_filters=self.feature_sub_depths,
            sub_layers=2,
            virtual_batch_size=self.virtual_batch_size
            )
        return outputs

    def make_model_fn(self):
        def _model_fn(features, labels, mode, params):
            is_train = (mode == tf.estimator.ModeKeys.TRAIN)
            mmsis = features['mmsi']
            time_ranges = features['time_ranges']
            timestamps = features['timestamps']
            features = features['features']
            self._build_net(features, timestamps, mmsis, is_train)

            if mode == tf.estimator.ModeKeys.PREDICT:
                predictions = {
                    "mmsi" : mmsis,
                    "time_ranges" : time_ranges,
                    "timestamps" : timestamps
                    }
                for obj in self.training_objectives:
                    predictions[obj.name] = obj.prediction
                return tf.estimator.EstimatorSpec(mode=mode, predictions=predictions)

            global_step = tf.train.get_global_step()

            total_loss = 0
            for obj in self. training_objectives:
                total_loss += obj.create_loss(labels[obj.name])

            learning_rate = tf.train.exponential_decay(
                self.initial_learning_rate, global_step, 
                self.decay_examples, self.learning_decay_rate)

            if mode == tf.estimator.ModeKeys.TRAIN:
                optimizer = tf.train.AdamOptimizer(learning_rate=learning_rate)

                update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
                with tf.control_dependencies(update_ops):
                    train_op = optimizer.minimize(loss=total_loss, 
                                                  global_step=global_step)

                return tf.estimator.EstimatorSpec(
                    mode=mode, loss=total_loss, train_op=train_op)

            assert mode == tf.estimator.ModeKeys.EVAL

            eval_metrics = {}
            for obj in self.training_objectives:
                eval_metrics.update(obj.create_metrics(labels[obj.name]))

            return tf.estimator.EstimatorSpec(
              mode=mode,
              loss=total_loss,
              eval_metric_ops=eval_metrics)
        return _model_fn

    def make_estimator(self, checkpoint_dir):
        session_config = tf.ConfigProto(allow_soft_placement=True)
        return  tf.estimator.Estimator(
            config=tf.estimator.RunConfig(
                            model_dir=checkpoint_dir, 
                            save_summary_steps=20,
                            save_checkpoints_secs=300, 
                            keep_checkpoint_max=10,
                            session_config=session_config),
            model_fn=self.make_model_fn(),
            params={
            })   

    def make_input_fn(self, base_feature_path, split, parallelism, prefetch):
        def input_fn():
            return (vessel_feature_generation.input_fn(
                        self.vessel_metadata,
                        self.build_training_file_list(base_feature_path, split),
                        self.num_feature_dimensions + 1,
                        self.max_window_duration_seconds,
                        self.window_max_points,
                        self.min_viable_timeslice_length,
                        objectives=self.training_objectives,
                        parallelism=parallelism)
                .prefetch(prefetch)
                .shuffle(prefetch)
                .batch(self.batch_size)
                )
        return input_fn

    def make_training_input_fn(self, base_feature_path, parallelism, prefetch=1024):
        return self.make_input_fn(base_feature_path, metadata.TRAINING_SPLIT, parallelism, prefetch)

    def make_test_input_fn(self, base_feature_path, parallelism, prefetch=1024):
        return self.make_input_fn(base_feature_path, metadata.TEST_SPLIT, parallelism, prefetch)

    def make_prediction_input_fn(self, paths, range_info, parallelism):
        time_ranges = range_info
        def input_fn():
            return vessel_feature_generation.predict_input_fn(
                            paths,
                            self.num_feature_dimensions + 1,
                            time_ranges,
                            self.window_max_points,
                            self.min_viable_timeslice_length,
                            parallelism=parallelism
                    ).batch(1)
        return input_fn

