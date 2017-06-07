import tensorflow as tf
from tensorflow.contrib import rnn
from seq2seq.embeddings import create_embedding_matrix


class DynamicCnnEncoder(object):
    def __init__(self, cell, bidirectional=False,
                 embedding_matrix=None, vocab_size=None, embedding_size=None,
                 special=None, defaults=None):
        assert embedding_matrix is not None \
               or (vocab_size is not None and embedding_size is not None)

        self.loss = None
        self.optimizer = None
        self.train_op = None

        self.cell = cell
        self.bidirectional = bidirectional
        self.special = special or {}

        self.scope = self.special.get("scope", "DynamicCnnEncoder")
        self.reuse_scope = self.special.get("reuse_scope", False)
        with tf.variable_scope(self.scope, self.reuse_scope):
            if embedding_matrix is not None:
                self.vocab_size, self.embedding_size = embedding_matrix.get_shape().as_list()
                self.embedding_matrix = embedding_matrix
            else:
                self.vocab_size = vocab_size
                self.embedding_size = embedding_size
                self.embedding_matrix = create_embedding_matrix(
                    self.vocab_size, self.embedding_size)

            self._build_graph(defaults)

    def _build_graph(self, defaults=None):
        if defaults is None:
            self.inputs = tf.placeholder(
                shape=(None, None),
                dtype=tf.int32,
                name="encoder_inputs")
            self.inputs_length = tf.placeholder(
                shape=(None,),
                dtype=tf.int32,
                name="encoder_inputs_length")
        else:
            default_inputs, default_inputs_length = defaults
            self.inputs = tf.placeholder_with_default(
                default_inputs,
                shape=(None, None),
                name="encoder_inputs")
            self.inputs_length = tf.placeholder_with_default(
                default_inputs_length,
                shape=(None,),
                name="encoder_inputs_length")

        self.global_step = tf.Variable(0, name="global_step", trainable=False)

        with tf.variable_scope("embedding") as scope:
            self.inputs_embedded = tf.nn.embedding_lookup(
                self.embedding_matrix, self.inputs)

        if self.bidirectional:
            with tf.variable_scope("BidirectionalEncoder") as scope:
                ((encoder_fw_outputs,
                  encoder_bw_outputs),
                 (encoder_fw_state,
                  encoder_bw_state)) = (
                    tf.nn.bidirectional_dynamic_rnn(
                        cell_fw=self.cell,
                        cell_bw=self.cell,
                        inputs=self.inputs_embedded,
                        sequence_length=self.inputs_length,
                        time_major=True,
                        dtype=tf.float32)
                )

                self.outputs = tf.concat(
                    (encoder_fw_outputs, encoder_bw_outputs), 2,
                    name="bidirectional_output_concat")

                if isinstance(encoder_fw_state, rnn.LSTMStateTuple):  # for LSTM cell
                    state_c = tf.concat(
                        (encoder_fw_state.c, encoder_bw_state.c), 1, name="bidirectional_concat_c")
                    state_h = tf.concat(
                        (encoder_fw_state.h, encoder_bw_state.h), 1, name="bidirectional_concat_h")
                    self.state = rnn.LSTMStateTuple(c=state_c, h=state_h)
                elif isinstance(encoder_fw_state, tuple) \
                        and isinstance(encoder_fw_state[0], rnn.LSTMStateTuple):
                    self.state = tuple(map(
                        lambda fw_state, bw_state: rnn.LSTMStateTuple(
                            c=tf.concat((fw_state.c, bw_state.c), 1,
                                        name="bidirectional_concat_c"),
                            h=tf.concat((fw_state.h, bw_state.h), 1,
                                        name="bidirectional_concat_h")),
                        encoder_fw_state, encoder_bw_state))
                else:
                    self.state = tf.concat(
                        (encoder_fw_state, encoder_bw_state), 1,
                        name="bidirectional_state_concat")
        else:
            with tf.variable_scope("Encoder") as scope:
                outputs, state = \
                    tf.nn.dynamic_rnn(
                        cell=self.cell,
                        inputs=self.inputs_embedded,
                        sequence_length=self.inputs_length,
                        time_major=True,
                        dtype=tf.float32)

                self.outputs = outputs
                self.state = state