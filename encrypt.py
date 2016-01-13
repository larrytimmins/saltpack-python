#! /usr/bin/env python3

import binascii
import io
import json
import os
import sys

import umsgpack
import libnacl
import docopt

import armor

__doc__ = '''\
Usage:
    encrypt.py encrypt [<private>] [<recipients>...] [options]
    encrypt.py decrypt [<private>] [options]

If no private key is given, the default is 32 zero bytes. If no recipients are
given, the default is the sender's own public key.

Options:
    -a --armor          encode/decode with saltpack armor
    -c --chunk=<size>   size of payload chunks, default 1 MB
    -m --message=<msg>  message text, instead of reading stdin
    --debug             debug mode
'''

FORMAT_VERSION = 1

DEBUG_MODE = False

# Hardcode the keys for everyone involved.
# ----------------------------------------

jack_private = b'\xaa' * 32


# Utility functions.
# ------------------

def chunks_with_empty(message, chunk_size):
    'The last chunk is empty, which signifies the end of the message.'
    chunk_start = 0
    chunks = []
    while chunk_start < len(message):
        chunks.append(message[chunk_start:chunk_start+chunk_size])
        chunk_start += chunk_size
    # empty chunk
    chunks.append(b'')
    return chunks


def json_repr(obj):
    # We need to repr everything that JSON doesn't directly support,
    # particularly bytes.
    def _recurse_repr(obj):
        if isinstance(obj, (list, tuple)):
            return [_recurse_repr(x) for x in obj]
        elif isinstance(obj, dict):
            return {_recurse_repr(key): _recurse_repr(val)
                    for key, val in obj.items()}
        elif isinstance(obj, bytes):
            try:
                obj.decode('utf8')
                return repr(obj)
            except UnicodeDecodeError:
                return tohex(obj)
        else:
            return obj
    return json.dumps(_recurse_repr(obj), indent='  ')


def tohex(b):
    return binascii.hexlify(b).decode()


def debug(*args):
    # hexify any bytes values
    args = list(args)
    for i, arg in enumerate(args):
        if isinstance(arg, bytes):
            args[i] = tohex(args[i])
    # print to stderr, if we're in debug mode
    if DEBUG_MODE:
        print(*args, file=sys.stderr)


# All the important bits!
# -----------------------

def encrypt(sender_private, recipient_public_keys, message, chunk_size):
    sender_public = libnacl.crypto_scalarmult_base(sender_private)
    ephemeral_private = os.urandom(32)
    ephemeral_public = libnacl.crypto_scalarmult_base(ephemeral_private)
    payload_key = os.urandom(32)

    sender_secretbox = libnacl.crypto_secretbox(
        msg=sender_public,
        nonce=b"saltpack_sender_key\0\0\0\0\0",
        key=payload_key)

    recipient_pairs = []
    for recipient_public in recipient_public_keys:
        # The recipient box holds the sender's long-term public key and the
        # symmetric message encryption key. It's encrypted for each recipient
        # with the ephemeral private key.
        payload_key_box = libnacl.crypto_box(
            msg=payload_key,
            nonce=b"saltpack_payload_key\0\0\0\0",
            pk=recipient_public,
            sk=ephemeral_private)
        # None is for the recipient public key, which is optional.
        pair = [None, payload_key_box]
        recipient_pairs.append(pair)

    header = [
        "SaltBox",  # format name
        [1, 0],     # major and minor version
        0,          # mode (encryption, as opposed to signing/detached)
        ephemeral_public,
        sender_secretbox,
        recipient_pairs,
    ]
    header_bytes = umsgpack.packb(header)
    header_len = umsgpack.packb(len(header_bytes))
    header_hash = libnacl.crypto_hash(header_bytes)
    output = io.BytesIO()
    output.write(header_len)
    output.write(header_bytes)

    # Compute the per-user MAC keys.
    recipient_mac_keys = []
    mac_keys_nonce = header_hash[:24]
    for recipient_public in recipient_public_keys:
        mac_key_box = libnacl.crypto_box(
            msg=b'\0'*32,
            nonce=mac_keys_nonce,
            pk=recipient_public,
            sk=sender_private)
        mac_key = mac_key_box[16:48]
        recipient_mac_keys.append(mac_key)

    # Write the chunks.
    for chunknum, chunk in enumerate(chunks_with_empty(message, chunk_size)):
        payload_nonce = b"saltpack_payload" + chunknum.to_bytes(8, "big")
        payload_secretbox = libnacl.crypto_secretbox(
            msg=chunk,
            nonce=payload_nonce,
            key=payload_key)
        # Authenticate the hash of the payload for each recipient.
        payload_hash = libnacl.crypto_hash(
            header_hash + payload_nonce + payload_secretbox)
        hash_authenticators = []
        for mac_key in recipient_mac_keys:
            authenticator = libnacl.crypto_auth(payload_hash, mac_key)
            hash_authenticators.append(authenticator)
        packet = [
            hash_authenticators,
            payload_secretbox,
        ]
        output.write(umsgpack.packb(packet))

    return output.getvalue()


def decrypt(input, recipient_private):
    stream = io.BytesIO(input)
    # Parse the header.
    header_len = umsgpack.unpack(stream)
    debug('header len:', header_len)
    header_bytes = stream.read(header_len)
    header_hash = libnacl.crypto_hash(header_bytes)
    header = umsgpack.unpackb(header_bytes)
    debug('header:', json_repr(header))
    debug('header hash:', header_hash)
    [
        format_name,
        [major_version, minor_version],
        mode,
        ephemeral_public,
        sender_secretbox,
        recipient_pairs,
        *_,  # ignore additional elements
    ] = header
    ephemeral_beforenm = libnacl.crypto_box_beforenm(
        pk=ephemeral_public,
        sk=recipient_private)

    # Try decrypting each sender box, until we find the one that works.
    for recipient_index, pair in enumerate(recipient_pairs):
        [_, payload_key_box, *_] = pair
        try:
            payload_key = libnacl.crypto_box_open_afternm(
                ctxt=payload_key_box,
                nonce=b"saltpack_payload_key\0\0\0\0",
                k=ephemeral_beforenm)
            break
        except ValueError:
            continue
    else:
        raise RuntimeError('Failed to find matching recipient.')

    sender_public = libnacl.crypto_secretbox_open(
        ctxt=sender_secretbox,
        nonce=b"saltpack_sender_key\0\0\0\0\0",
        key=payload_key)

    mac_key_nonce = header_hash[:24]
    mac_key_box = libnacl.crypto_box(
        msg=b'\0'*32,
        nonce=mac_key_nonce,
        pk=sender_public,
        sk=recipient_private)
    mac_key = mac_key_box[16:48]

    debug('recipient index:', recipient_index)
    debug('sender key:', sender_public)
    debug('payload key:', payload_key)
    debug('mac key nonce:', mac_key_nonce)
    debug('mac key:', mac_key)

    # Decrypt each of the packets.
    output = io.BytesIO()
    chunknum = 0
    while True:
        packet = umsgpack.unpack(stream)
        debug('packet:', json_repr(packet))
        [hash_authenticators, payload_secretbox, *_] = packet
        hash_authenticator = hash_authenticators[recipient_index]

        # Verify the secretbox hash.
        payload_nonce = b"saltpack_payload" + chunknum.to_bytes(8, "big")
        debug('payload nonce:', payload_nonce)
        payload_hash = libnacl.crypto_hash(
            header_hash + payload_nonce + payload_secretbox)
        debug('hash to authenticate:', payload_hash)
        libnacl.crypto_auth_verify(
            tok=hash_authenticator,
            msg=payload_hash,
            key=mac_key)

        # Open the payload secretbox.
        chunk = libnacl.crypto_secretbox_open(
            ctxt=payload_secretbox,
            nonce=payload_nonce,
            key=payload_key)
        output.write(chunk)

        debug('chunk:', repr(chunk))

        # The empty chunk signifies the end of the message.
        if chunk == b'':
            break

        chunknum += 1

    return output.getvalue()


def get_private(args):
    if args['<private>']:
        private = binascii.unhexlify(args['<private>'])
        assert len(private) == 32
        return private
    else:
        return b'\0'*32


def get_recipients(args):
    if args['<recipients>']:
        recipients = []
        for recipient in args['<recipients>']:
            key = binascii.unhexlify(recipient)
            assert len(key) == 32
            recipients.append(key)
        return recipients
    else:
        # Without explicit recipients, just send to yourself.
        private = get_private(args)
        public = libnacl.crypto_scalarmult_base(private)
        return [public]


def do_encrypt(args):
    message = args['--message']
    if message is None:
        encoded_message = sys.stdin.buffer.read()
    else:
        encoded_message = message.encode('utf8')
    sender = get_private(args)
    if args['--chunk']:
        chunk_size = int(args['--chunk'])
    else:
        chunk_size = 10**6
    recipients = get_recipients(args)
    output = encrypt(
        sender,
        recipients,
        encoded_message,
        chunk_size)
    if args['--armor']:
        output = (armor.armor(output) + '\n').encode()
    sys.stdout.buffer.write(output)


def do_decrypt(args):
    message = sys.stdin.buffer.read()
    if args['--armor']:
        message = armor.dearmor(message.decode())
    private = get_private(args)
    decoded_message = decrypt(message, private)
    sys.stdout.buffer.write(decoded_message)


def main():
    global DEBUG_MODE
    args = docopt.docopt(__doc__)
    DEBUG_MODE = args['--debug']
    if args['encrypt']:
        do_encrypt(args)
    else:
        do_decrypt(args)


if __name__ == '__main__':
    main()
