# coding: utf-8
import tensorflow as tf
import numpy as np
import os
from collections import Counter
import librosa
import codecs
import pickle
import json

from joblib import Parallel, delayed

from collections import namedtuple

PAD = '<PAD>'
UNK = '<UNK>'
PAD_ID = 0
UNK_ID = 1

HParams = namedtuple('HParams',
                     ' batch_size,vocab_size,lr,min_lr'
                     'wavs_list_path,labels_vec_path,'
                     'label_max_len,wav_max_len,n_mfcc,'
                     'mode'
                     )


class Model(object):
    def __init__(self, hps):
        self.aconv1d_index = 0
        self.conv1d_index = 0
        self.hps = hps
        self.logit = None
        self.loss = None

    def conv1d_layer(self, input_tensor, size, dim, activation, scale, bias):
        with tf.variable_scope("conv1d_" + str(self.conv1d_index)):
            W = tf.get_variable('W', (size, input_tensor.get_shape().as_list()[-1], dim), dtype=tf.float32,
                                initializer=tf.random_uniform_initializer(minval=-scale, maxval=scale))
            if bias:
                b = tf.get_variable('b', [dim], dtype=tf.float32, initializer=tf.constant_initializer(0))
            out = tf.nn.conv1d(input_tensor, W, stride=1, padding='SAME') + (b if bias else 0)

            if not bias:
                beta = tf.get_variable('beta', dim, dtype=tf.float32, initializer=tf.constant_initializer(0))
                gamma = tf.get_variable('gamma', dim, dtype=tf.float32, initializer=tf.constant_initializer(1))
                mean_running = tf.get_variable('mean', dim, dtype=tf.float32,
                                               initializer=tf.constant_initializer(0))
                variance_running = tf.get_variable('variance', dim, dtype=tf.float32,
                                                   initializer=tf.constant_initializer(1))
                mean, variance = tf.nn.moments(out, axes=list(range(len(out.get_shape()) - 1)))

                def update_running_stat():
                    decay = 0.99

                    # 定义了均值方差指数衰减 见 http://blog.csdn.net/liyuan123zhouhui/article/details/70698264
                    update_op = [mean_running.assign(mean_running * decay + mean * (1 - decay)),
                                 variance_running.assign(variance_running * decay + variance * (1 - decay))]

                    # 指定先执行均值方差的更新运算 见 http://blog.csdn.net/u012436149/article/details/72084744
                    with tf.control_dependencies(update_op):
                        return tf.identity(mean), tf.identity(variance)

                        # 条件运算(https://applenob.github.io/tf_9.html) 按照作者这里的指定 是不进行指数衰减的

                m, v = tf.cond(tf.Variable(False, trainable=False), update_running_stat,
                               lambda: (mean_running, variance_running))
                out = tf.nn.batch_normalization(out, m, v, beta, gamma, 1e-8)

            if activation == 'tanh':
                out = tf.nn.tanh(out)
            elif activation == 'sigmoid':
                out = tf.nn.sigmoid(out)

            self.conv1d_index += 1
            return out

            # 极黑卷积层 https://www.zhihu.com/question/57414498

    def aconv1d_layer(self, input_tensor, size, rate, activation, scale, bias):
        with tf.variable_scope('aconv1d_' + str(self.aconv1d_index)):
            shape = input_tensor.get_shape().as_list()

            # 利用 2 维极黑卷积函数计算相应 1 维卷积，expand_dims squeeze做了相应维度处理
            # 实际 上一个 tf.nn.conv1d 在之前的tensorflow版本中是没有的，其的一个实现也是经过维度调整后调用 tf.nn.conv2d
            W = tf.get_variable('W', (1, size, shape[-1], shape[-1]), dtype=tf.float32,
                                initializer=tf.random_uniform_initializer(minval=-scale, maxval=scale))
            if bias:
                b = tf.get_variable('b', [shape[-1]], dtype=tf.float32, initializer=tf.constant_initializer(0))
            out = tf.nn.atrous_conv2d(tf.expand_dims(input_tensor, dim=1), W, rate=rate, padding='SAME')
            out = tf.squeeze(out, [1])

            if not bias:
                beta = tf.get_variable('beta', shape[-1], dtype=tf.float32, initializer=tf.constant_initializer(0))
                gamma = tf.get_variable('gamma', shape[-1], dtype=tf.float32,
                                        initializer=tf.constant_initializer(1))
                mean_running = tf.get_variable('mean', shape[-1], dtype=tf.float32,
                                               initializer=tf.constant_initializer(0))
                variance_running = tf.get_variable('variance', shape[-1], dtype=tf.float32,
                                                   initializer=tf.constant_initializer(1))
                mean, variance = tf.nn.moments(out, axes=list(range(len(out.get_shape()) - 1)))

                def update_running_stat():
                    decay = 0.99
                    update_op = [mean_running.assign(mean_running * decay + mean * (1 - decay)),
                                 variance_running.assign(variance_running * decay + variance * (1 - decay))]
                    with tf.control_dependencies(update_op):
                        return tf.identity(mean), tf.identity(variance)

                m, v = tf.cond(tf.Variable(False, trainable=False), update_running_stat,
                               lambda: (mean_running, variance_running))
                out = tf.nn.batch_normalization(out, m, v, beta, gamma, 1e-8)

            if activation == 'tanh':
                out = tf.nn.tanh(out)
            elif activation == 'sigmoid':
                out = tf.nn.sigmoid(out)

            self.aconv1d_index += 1
            return out

    def _build_neural_layer(self, n_dim=128, n_blocks=3):
        hp = self.hps
        out = self.conv1d_layer(input_tensor=self.X, size=1, dim=n_dim, activation='tanh', scale=0.14, bias=False)

        def residual_block(input_sensor, size, rate):
            conv_filter = self.aconv1d_layer(input_tensor=input_sensor, size=size, rate=rate, activation='tanh',
                                             scale=0.03,
                                             bias=False)
            conv_gate = self.aconv1d_layer(input_tensor=input_sensor, size=size, rate=rate, activation='sigmoid',
                                           scale=0.03,
                                           bias=False)
            out = conv_filter * conv_gate
            out = self.conv1d_layer(out, size=1, dim=n_dim, activation='tanh', scale=0.08, bias=False)
            return out + input_sensor, out

        skip = 0
        for _ in range(n_blocks):
            for r in [1, 2, 4, 8, 16]:
                out, s = residual_block(out, size=7, rate=r)
                skip += s

        logit = self.conv1d_layer(skip, size=1, dim=skip.get_shape().as_list()[-1], activation='tanh', scale=0.08,
                                  bias=False)

        # 最后卷积层输出是词汇表大小
        self.logit = self.conv1d_layer(logit, size=1, dim=hp.vocab_size, activation=None, scale=0.04, bias=True)

    def _add_loss(self):
        hps = self.hps
        indices = tf.where(tf.not_equal(tf.cast(self.Y, tf.float32), 0.))
        target = tf.SparseTensor(indices=indices, values=tf.gather_nd(self.Y, indices) - 1,
                                 dense_shape=tf.cast(tf.shape(self.Y), tf.int64))
        self.sequence_len = tf.reduce_sum(
            tf.cast(tf.not_equal(tf.reduce_sum(self.X, reduction_indices=2), 0.), tf.int32),
            reduction_indices=1)
        self.loss = tf.nn.ctc_loss(target, self.logit, self.sequence_len, time_major=False)
        self.batch_loss = tf.div(tf.reduce_sum(self.loss), hps.batch_size, name="batch_loss")

    def _add_placeholders(self):
        hps = self.hps
        self.X = tf.placeholder(dtype=tf.float32, shape=[hps.batch_size, hps.wav_max_len, hps.n_mfcc],
                                name='placeholder_x')
        self.Y = tf.placeholder(dtype=tf.int32, shape=[hps.batch_size, hps.label_max_len], name='placeholder_y')

    def _add_train_op(self):
        hps = self.hps
        self._lr_rate = tf.maximum(
            hps.min_lr,  # min_lr_rate.
            tf.train.exponential_decay(hps.lr, self.global_step, 30000, 0.98))
        optimizer = MaxPropOptimizer(learning_rate=self._lr_rate, beta2=0.99)
        var_list = [t for t in tf.trainable_variables()]
        gradient = optimizer.compute_gradients(self.loss, var_list=var_list)
        self.optimizer_op = optimizer.apply_gradients(gradient)
        self.global_step = self.global_step.assign_add(1)

    def _add_decode_op(self):
        decoded = tf.transpose(self.logit, perm=[1, 0, 2])
        decoded, _ = tf.nn.ctc_beam_search_decoder(decoded, self.sequence_len, merge_repeated=False)
        self.predict = tf.sparse_to_dense(decoded[0].indices, decoded[0].dense_shape, decoded[0].values) + 1

    def build_model(self):
        self.global_step = tf.Variable(0, name='global_step', trainable=False)
        self._add_placeholders()
        self._build_neural_layer()
        self._add_loss()
        if self.hps.mode == 'train':
            self._add_train_op()
        elif self.hps.mode == 'infer':
            self._add_decode_op()

    def run_train_step(self, sess, x_batch, y_batch):
        to_return = [self.optimizer_op,  self.batch_loss, self.global_step]
        print(x_batch.shape, x_batch.dtype, ':-------x_batch')
        print(y_batch.shape, y_batch.dtype, '---------y_batch')
        return sess.run(to_return, feed_dict={self.X: x_batch, self.Y: y_batch})

    def run_infer(self, sess, mfcc):
        return sess.run(self.predict, {self.X: mfcc})


class MaxPropOptimizer(tf.train.Optimizer):
    def __init__(self, learning_rate=0.001, beta2=0.999, use_locking=False, name="MaxProp"):
        super(MaxPropOptimizer, self).__init__(use_locking, name)
        self._lr = learning_rate
        self._beta2 = beta2
        self._lr_t = None
        self._beta2_t = None

    def _prepare(self):
        self._lr_t = tf.convert_to_tensor(self._lr, name="learning_rate")
        self._beta2_t = tf.convert_to_tensor(self._beta2, name="beta2")

    def _create_slots(self, var_list):
        for v in var_list:
            self._zeros_slot(v, "m", self._name)

    def _apply_dense(self, grad, var):
        lr_t = tf.cast(self._lr_t, var.dtype.base_dtype)
        beta2_t = tf.cast(self._beta2_t, var.dtype.base_dtype)
        if var.dtype.base_dtype == tf.float16:
            eps = 1e-7
        else:
            eps = 1e-8
        m = self.get_slot(var, "m")
        m_t = m.assign(tf.maximum(beta2_t * m + eps, tf.abs(grad)))
        g_t = grad / m_t
        var_update = tf.assign_sub(var, lr_t * g_t)
        return tf.group(*[var_update, m_t])

    def _apply_sparse(self, grad, var):
        return self._apply_dense(grad, var)


class Batcher(object):
    def __init__(self, hps):
        self.wav_file_paths = json.load(codecs.open(hps.wavs_list_path, 'r', encoding='utf-8'))
        self.labels_id_vectors = json.load(codecs.open(hps.labels_vec_path, 'r', encoding='utf-8'))
        self.pointer = 0
        self.batch_size = hps.batch_size
        self.wav_max_len = hps.wav_max_len
        self.label_max_len = hps.label_max_len
        self.n_mfcc = hps.n_mfcc

    def get_next_batches(self):
        total_length = len(self.labels_id_vectors)
        batches_wavs, batches_labels = [], []
        for i in range(self.batch_size):
            wav, sr = librosa.load(self.wav_file_paths[self.pointer])
            mfcc = np.transpose(librosa.feature.mfcc(wav, sr, n_mfcc=self.n_mfcc), [1, 0])
            batches_wavs.append(mfcc.tolist())
            batches_labels.append(self.labels_id_vectors[self.pointer])
            self.pointer = (self.pointer + 1) % total_length
            # 取零补齐
        # label append 0 , 0 对应的字符
        # mfcc 默认的计算长度为20(n_mfcc of mfcc) 作为channel length
        for mfcc in batches_wavs:
            while len(mfcc) < self.wav_max_len:
                mfcc.append([PAD_ID] * self.n_mfcc)
        for label in batches_labels:
            while len(label) < self.label_max_len:
                label.append(PAD_ID)
        rs_x = np.array(batches_wavs, dtype=np.float32)
        rs_y = np.array(batches_labels, dtype=np.int32)
        return rs_x, rs_y
