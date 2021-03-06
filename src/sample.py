import tensorflow as tf

from . import model

def top_k_logits(logits, k):
    if k == 0:
        # no truncation
        return logits

    def _top_k():
        values, _ = tf.nn.top_k(logits, k=k)
        min_values = values[:, -1, tf.newaxis]
        return tf.where(
            logits < min_values,
            tf.ones_like(logits, dtype=logits.dtype) * -1e10,
            logits,
        )
    return tf.cond(
       tf.equal(k, 0),
       lambda: logits,
       lambda: _top_k(),
    )


def top_p_logits(logits, p):
    """Nucleus sampling"""
    batch, _ = logits.shape.as_list()
    sorted_logits = tf.sort(logits, direction='DESCENDING', axis=-1)
    cumulative_probs = tf.cumsum(tf.nn.softmax(sorted_logits, axis=-1), axis=-1)
    indices = tf.stack([
        tf.range(0, batch),
        # number of indices to include
        tf.maximum(tf.reduce_sum(tf.cast(cumulative_probs <= p, tf.int32), axis=-1) - 1, 0),
    ], axis=-1)
    min_values = tf.gather_nd(sorted_logits, indices)
    return tf.where(
        logits < min_values,
        tf.ones_like(logits) * -1e10,
        logits,
    )

# BCD 2020/07/05
def restricted_logits(logits, allowed_tokens):
    """
    Restrict chosen tokens to those for which allowed_tokens > 0
    """
    if allowed_tokens == None:
        # no restriction
        return logits

    return tf.where(
        allowed_tokens < 1,
        tf.ones_like(logits) * -1e10,
        logits,
    )

def sample_sequence(*, hparams, length, start_token=None, batch_size=None, context=None, temperature=1, top_k=0, top_p=1, allowed_tokens_list=None, word_start_tokens=None,
    word_start_tokens_dense=None, word_end_tokens=None, reweight=None):
    if start_token is None:
        assert context is not None, 'Specify exactly one of start_token and context!'
    else:
        assert context is None, 'Specify exactly one of start_token and context!'
        context = tf.fill([batch_size, 1], start_token)

    def step(hparams, tokens, past=None):
        lm_output = model.model(hparams=hparams, X=tokens, past=past, reuse=tf.AUTO_REUSE)

        logits = lm_output['logits'][:, :, :hparams.n_vocab]
        presents = lm_output['present']
        presents.set_shape(model.past_shape(hparams=hparams, batch_size=batch_size))
        return {
            'logits': logits,
            'presents': presents,
        }

    with tf.name_scope('sample_sequence'):
    
        # BCD 2020/07/25
        def count_words(tokens, word_start_tokens):
            if tokens is None:
                return 0
            else:
                return tf.size(tf.sets.intersection(tokens,word_start_tokens))
                
        # BCD 2020/08/08
        def last_is_end_token(tokens, word_end_tokens):
            """
            Returns 1 if final token is in "word_end_tokens', 0 otherwise.
            """
            return tf.dtypes.cast(
                tf.size(tf.sets.intersection(tokens[:,-1:],word_end_tokens[:])) > 0,
                tf.float32)
                        
        def body(past, prev, output):
            next_outputs = step(hparams, prev, past=past)
            logits = next_outputs['logits'][:, -1, :]  / tf.to_float(temperature)
            logits = top_k_logits(logits, k=top_k)
            logits = top_p_logits(logits, p=top_p)
            
            # BCD stuff
            num_prev_words = count_words(output,word_start_tokens)
            idx = num_prev_words % tf.shape(allowed_tokens_list)[1]
            logits = restricted_logits(logits,
                        allowed_tokens=allowed_tokens_list[:,idx])
            # further restrict by requiring token after a "word_end"
            # token to be a "word_start" token (does nothing if
            # last_is_end_token is false)
            r = -1e10*last_is_end_token(output,word_end_tokens)*(1.-tf.dtypes.cast(word_start_tokens_dense,tf.float32))
            # and allow for final arbitrary reweighting
            if reweight is not None:
                logits = logits + reweight + r
            
            samples = tf.multinomial(logits, num_samples=1, output_dtype=tf.int32)
            return [
                next_outputs['presents'] if past is None else tf.concat([past, next_outputs['presents']], axis=-2),
                samples,
                tf.concat([output, samples], axis=1)
            ]

        past, prev, output = body(None, context, context)

        def cond(*args):
            return True

        _, _, tokens = tf.while_loop(
            cond=cond, body=body,
            maximum_iterations=length - 1,
            loop_vars=[
                past,
                prev,
                output
            ],
            shape_invariants=[
                tf.TensorShape(model.past_shape(hparams=hparams, batch_size=batch_size)),
                tf.TensorShape([batch_size, None]),
                tf.TensorShape([batch_size, None]),
            ],
            back_prop=False,
        )

        return tokens
