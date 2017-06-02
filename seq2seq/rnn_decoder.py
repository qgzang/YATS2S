import tensorflow as tf
from tensorflow.contrib import layers
from tensorflow.contrib import seq2seq
from seq2seq.embeddings import create_embedding_matrix


class DynamicRnnDecoder(object):
    def __init__(self, cell, encoder_state, encoder_outputs,
                 maximum_length=150,
                 attention=False,
                 embedding_matrix=None, vocab_size=None, embedding_size=None,
                 special=None, defaults=None):
        assert embedding_matrix is not None \
               or (vocab_size is not None and embedding_size is not None)

        self.loss = None
        self.optimizer = None
        self.train_op = None
        self.cell = cell
        self.encoder_state = encoder_state
        # @TODO: should be optimal
        self.encoder_outputs = encoder_outputs
        self.maximum_length = maximum_length
        self.attention = attention

        self.special = special or {}
        self.PAD = self.special.get("PAD", 0)
        self.EOS = self.special.get("EOS", 1)

        self.scope = self.special.get("scope", "DynamicRnnDecoder")
        self.reuse_scope = self.special.get("reuse_scope", False)
        with tf.variable_scope(self.scope, self.reuse_scope):
            # @TODO: should work without all encoder stuff ?
            if embedding_matrix is not None:
                self.vocab_size, self.embedding_size = embedding_matrix.get_shape().as_list()
                self.embedding_matrix = embedding_matrix
            else:
                self.vocab_size = vocab_size
                self.embedding_size = embedding_size
                self.embedding_matrix = create_embedding_matrix(
                    self.vocab_size, self.embedding_size)

            self._build_graph(defaults)
            self._build_loss()

    @property
    def decoder_hidden_units(self):
        if isinstance(self.cell.output_size, tuple):
            return self.cell.output_size[0]  # LSTM support? need to test
        else:
            return self.cell.output_size

    def _build_graph(self, defaults=None):
        # required only for training
        if defaults is None:
            self.targets = tf.placeholder(
                shape=(None, None),
                dtype=tf.int32,
                name="decoder_inputs")
            self.targets_length = tf.placeholder(
                shape=(None,),
                dtype=tf.int32,
                name="decoder_inputs_length")
        else:
            default_targets, default_targets_length = defaults
            self.targets = tf.placeholder_with_default(
                default_targets,
                shape=(None, None),
                name="decoder_inputs")
            self.targets_length = tf.placeholder_with_default(
                default_targets_length,
                shape=(None,),
                name="decoder_inputs_length")
        self.global_step = tf.Variable(0, name="global_step", trainable=False)

        sequence_size, batch_size = tf.unstack(tf.shape(self.targets))
        if self.special.get("predict_sequence", True):
            with tf.name_scope("DecoderTrainFeed"):
                EOS_SLICE = tf.ones([1, batch_size], dtype=tf.int32) * self.EOS
                PAD_SLICE = tf.ones([1, batch_size], dtype=tf.int32) * self.PAD

                self.train_inputs = tf.concat([EOS_SLICE, self.targets], axis=0)
                self.train_length = self.targets_length + 1

                train_targets = tf.concat([self.targets, PAD_SLICE], axis=0)
                train_targets_seq_len, _ = tf.unstack(tf.shape(train_targets))
                train_targets_eos_mask = tf.one_hot(
                    self.train_length - 1,
                    train_targets_seq_len,
                    on_value=self.EOS, off_value=self.PAD,
                    dtype=tf.int32)
                train_targets_eos_mask = tf.transpose(train_targets_eos_mask, [1, 0])

                # hacky way using one_hot to put EOS symbol at the end of target sequence
                train_targets = tf.add(
                    train_targets, train_targets_eos_mask)

                self.train_targets = train_targets
        else:
            self.train_inputs = self.targets
            self.train_targets = self.targets
            self.train_length = self.targets_length

        self.loss_weights = tf.ones([
            batch_size,
            tf.reduce_max(self.train_length)],
            dtype=tf.float32, name="loss_weights")

        with tf.variable_scope("embedding") as scope:
            self.inputs_embedded = tf.nn.embedding_lookup(
                self.embedding_matrix, self.train_inputs)

        with tf.variable_scope("Decoder") as scope:

            def logits_fn(outputs):
                return layers.linear(outputs, self.vocab_size, scope=scope)

            if not self.attention:
                train_fn = seq2seq.simple_decoder_fn_train(
                    encoder_state=self.encoder_state)
                inference_fn = seq2seq.simple_decoder_fn_inference(
                    output_fn=logits_fn,
                    encoder_state=self.encoder_state,
                    embeddings=self.embedding_matrix,
                    start_of_sequence_id=self.EOS,
                    end_of_sequence_id=self.EOS,
                    maximum_length=self.maximum_length,
                    num_decoder_symbols=self.vocab_size)
            else:

                # attention_states: size [batch_size, max_time, num_units]
                attention_states = tf.transpose(self.encoder_outputs, [1, 0, 2])

                (attention_keys,
                 attention_values,
                 attention_score_fn,
                 attention_construct_fn) = seq2seq.prepare_attention(
                    attention_states=attention_states,
                    attention_option="bahdanau",
                    num_units=self.decoder_hidden_units)

                train_fn = seq2seq.attention_decoder_fn_train(
                    encoder_state=self.encoder_state,
                    attention_keys=attention_keys,
                    attention_values=attention_values,
                    attention_score_fn=attention_score_fn,
                    attention_construct_fn=attention_construct_fn,
                    name="decoder_attention")

                inference_fn = seq2seq.attention_decoder_fn_inference(
                    output_fn=logits_fn,
                    encoder_state=self.encoder_state,
                    attention_keys=attention_keys,
                    attention_values=attention_values,
                    attention_score_fn=attention_score_fn,
                    attention_construct_fn=attention_construct_fn,
                    embeddings=self.embedding_matrix,
                    start_of_sequence_id=self.EOS,
                    end_of_sequence_id=self.EOS,
                    maximum_length=self.maximum_length,
                    num_decoder_symbols=self.vocab_size)

            (self.train_outputs,
             self.train_state,
             self.train_context_state) = seq2seq.dynamic_rnn_decoder(
                cell=self.cell,
                decoder_fn=train_fn,
                inputs=self.inputs_embedded,
                sequence_length=self.train_length,
                time_major=True,
                scope=scope)

            self.train_logits = logits_fn(self.train_outputs)
            self.train_prediction = tf.argmax(
                self.train_logits, axis=-1,
                name="train_prediction")
            self.train_prediction_probabilities = tf.nn.softmax(
                self.train_logits, dim=-1,
                name="train_prediction_probabilities")

            scope.reuse_variables()

            (self.inference_logits,
             self.inference_state,
             self.inference_context_state) = seq2seq.dynamic_rnn_decoder(
                cell=self.cell,
                decoder_fn=inference_fn,
                time_major=True,
                scope=scope)

            self.inference_prediction = tf.argmax(
                self.inference_logits, axis=-1,
                name="inference_prediction")
            self.inference_prediction_probabilities = tf.nn.softmax(
                self.inference_logits, dim=-1,
                name="inference_prediction_probabilities")

    def _build_loss(self):
        self.train_logits_seq = tf.transpose(self.train_logits, [1, 0, 2])
        self.train_targets_seq = tf.transpose(self.train_targets, [1, 0])
        loss_type = self.special.get("loss_type", "cross_entropy")

        if loss_type == "cross_entropy":
            self.unreg_loss = self.loss = seq2seq.sequence_loss(
                logits=self.train_logits_seq, targets=self.train_targets_seq,
                weights=self.loss_weights)
        elif loss_type == "hinge":
            self.train_logits_seq = tf.squeeze(self.train_logits_seq, [1])
            self.train_targets_seq = tf.squeeze(self.train_targets_seq, [1])
            self.unreg_loss = self.loss = tf.losses.hinge_loss(
                labels=tf.cast(tf.one_hot(self.train_targets_seq, self.vocab_size), tf.float32),
                logits=self.train_logits_seq)
        elif loss_type == "contrastive":
            from seq2seq.contrib.losses import contrastive_loss
            margin = self.special.get("contrastive_margin", 1.0)
            self.train_logits_seq = tf.squeeze(self.train_logits_seq, [1])
            self.train_targets_seq = tf.squeeze(self.train_targets_seq, [1])
            self.train_prediction_probabilities_seq = tf.nn.softmax(
                self.train_logits_seq, dim=-1,
                name="train_prediction_probabilities_seq")
            self.unreg_loss = self.loss = contrastive_loss(
                labels=tf.cast(tf.one_hot(self.train_targets_seq, self.vocab_size), tf.float32),
                distances=self.train_prediction_probabilities_seq,
                margin=margin)

