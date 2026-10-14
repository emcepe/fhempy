import asyncio
import binascii
import concurrent.futures
import inspect
import json
import os
import socket
from base64 import urlsafe_b64decode, urlsafe_b64encode
from codecs import decode
from datetime import datetime
from functools import partial, reduce

from . import fhem


def encrypt_string(plain_text, fhem_unique_id):
    from Cryptodome.Cipher import AES

    key = fhem_unique_id.encode("utf-8")
    b_text = plain_text.encode("utf-8")
    e_cipher = AES.new(key, AES.MODE_EAX, nonce=key[0:16])
    e_data = e_cipher.encrypt(b_text)
    return "crypt-aes:" + urlsafe_b64encode(e_data).decode()


def decrypt_string(encrypted_text, fhem_unique_id):
    if encrypted_text[0:10] != "crypt-aes:":
        return decrypt_fernet(encrypted_text, fhem_unique_id)
    from Cryptodome.Cipher import AES

    key = fhem_unique_id.encode("utf-8")
    encrypted_data = urlsafe_b64decode(encrypted_text[10:])
    d_cipher = AES.new(key, AES.MODE_EAX, nonce=key[0:16])
    return d_cipher.decrypt(encrypted_data).decode()


def decrypt_fernet(token_b64, key_str):
    from Cryptodome.Cipher import AES
    from Cryptodome.Hash import HMAC, SHA256
    from Cryptodome.Util.Padding import unpad

    try:
        keys = key_str.encode("utf-8")
        token_z = token_b64.encode("utf-8")
        token_b64 = reduce(decode, ("base64", "zlib"), token_z).decode()
        token = urlsafe_b64decode(token_b64)

        if len(keys) != 32 or len(token) < 73 or token[0] != 0x80:
            raise ValueError("Incorrect format")

        hmac_key, dec_key = keys[:16], keys[16:]
        HMAC.new(hmac_key, token[:-32], digestmod=SHA256).verify(token[-32:])
        iv, ct = token[9:25], token[25:-32]
        # Possibly check time-to-live here
        message = unpad(AES.new(dec_key, AES.MODE_CBC, iv=iv).decrypt(ct), 16).decode()

    except (ValueError, binascii.Error):
        message = None

    return message


def get_fhempy_root():
    from fhempy import lib

    initfile = inspect.getfile(lib)
    fhempy_root = os.path.dirname(initfile)
    return fhempy_root


async def run_blocking(function):
    if isinstance(function, partial) is False:
        raise Exception("Use functools.partial to call run_blocking")

    pool = concurrent.futures.ThreadPoolExecutor()
    try:
        return await asyncio.get_event_loop().run_in_executor(pool, function)
    finally:
        # don't block the event loop if the task was cancelled (Undefine),
        # the thread finishes in the background
        pool.shutdown(wait=False)


def run_blocking_task(function):
    return asyncio.create_task(run_blocking(function))


# example config
# attr_list = {
#   "attribute1": {"default": 10, "format": "int", "options":"1,2,3"}
# }
async def handle_attr(attr_list, obj, hash, args, argsh):
    cmd = args[0]
    name = args[1]
    attr_name = args[2]
    attr_val = args[3]
    if attr_name in attr_list:
        if cmd == "set":
            setattr(
                obj,
                "_attr_" + attr_name,
                convert2format(attr_val, attr_list[attr_name]),
            )
        else:
            if "default" in attr_list[attr_name]:
                setattr(obj, "_attr_" + attr_name, attr_list[attr_name]["default"])
            else:
                setattr(obj, "_attr_" + attr_name, "")

    # call set_attr_....
    fct_name = "set_attr_" + attr_name
    ret = None
    if attr_name in attr_list and "function" in attr_list[attr_name]:
        fct_name = attr_list[attr_name]["function"]
    try:
        fct_call = getattr(obj, fct_name)
        ret = await fct_call(hash)
    except AttributeError:
        pass

    return ret


def get_local_ip():
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        # Use Google Public DNS server to determine own IP
        sock.connect(("8.8.8.8", 80))

        return sock.getsockname()[0]  # type: ignore
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except socket.gaierror:
            return "127.0.0.1"
    finally:
        if sock is not None:
            sock.close()


async def handle_define_attr(attr_list, obj, hash, extra_attrs=None):
    """Set the attribute list in FHEM and read all attribute values in one
    roundtrip. extra_attrs ({attribute: default}) are read as well.
    Returns the result of fhem.getDeviceInfo."""
    add_to_list = []
    for attr in attr_list:
        if "options" in attr_list[attr]:
            attr_opt = attr + ":" + attr_list[attr]["options"]
        else:
            attr_opt = attr
        add_to_list.append(attr_opt)

    attrs = {attr: "" for attr in attr_list}
    if extra_attrs:
        attrs.update(extra_attrs)
    info = await fhem.getDeviceInfo(hash["NAME"], attrs, " ".join(add_to_list))

    for attr in attr_list:
        curr_val = info["attr"][attr]
        if curr_val == "" and "default" in attr_list[attr]:
            curr_val = attr_list[attr]["default"]
        setattr(obj, "_attr_" + attr, convert2format(curr_val, attr_list[attr]))

    return info


def flatten_json(y):
    out = {}
    if type(y) is str:
        y = json.loads(y)

    def flatten(x, name=""):
        if type(x) is dict:
            for a in x:
                flatten(x[a], name + a + "_")
        elif type(x) is list:
            i = 0
            for a in x:
                flatten(a, name + str(i) + "_")
                i += 1
        else:
            out[name[:-1]] = x

    flatten(y)
    return out


def gen_reading_name(reading):
    reading = reading.lower()
    reading = reading.replace(" ", "_").replace("-", "_")
    return remove_special_charachters(reading)


def gen_fhemdev_name(devname):
    return remove_special_charachters(
        devname.replace(" ", "_").replace("-", "_").replace(":", "_")
    )


def remove_special_charachters(string):
    """
    Removes umlauts from strings and replaces them with the letter+e convention
    :param string: string to remove umlauts from
    :return: unumlauted string
    """
    u_enc = "ü".encode()
    U_enc = "Ü".encode()
    a_enc = "ä".encode()
    A_enc = "Ä".encode()
    o_enc = "ö".encode()
    O_enc = "Ö".encode()
    ss_enc = "ß".encode()

    string = string.encode()
    string = string.replace(u_enc, b"ue")
    string = string.replace(U_enc, b"Ue")
    string = string.replace(a_enc, b"ae")
    string = string.replace(A_enc, b"Ae")
    string = string.replace(o_enc, b"oe")
    string = string.replace(O_enc, b"Oe")
    string = string.replace(ss_enc, b"ss")

    string = string.decode("utf-8")

    string = string.replace("(", "")
    string = string.replace(")", "")
    string = string.replace("'", "")
    string = string.replace("`", "")
    string = string.replace('"', "")
    return string


def convert2format(value, list_def):
    target_format = "none"
    if "format" in list_def:
        target_format = list_def["format"]

    if target_format == "int":
        return int(value)
    elif target_format == "float":
        return float(value)
    elif target_format == "str":
        return str(value)
    elif target_format == "bool":
        if value == "on":
            return True
        else:
            return False
    elif target_format == "json":
        try:
            return json.loads(value)
        except Exception:
            return {}
    elif target_format == "time":
        try:
            return datetime.strptime(value, "%H:%M")
        except Exception:
            return None
    elif target_format == "array":
        return value.split(",")
    return value


# example config
# set_list_conf = {
#    "mode": { "args": ["mode"], "argsh": ["mode"], "params": { "mode": { "default": "eco", "optional": False }}, "options": "eco,comfort" },
#    "desiredTemp": { "args": ["temperature"], "options": "slider,10,1,30"},
#    "holidayMode": { "args": ["start", "end", "temperature"], "params": { "start": {"default": "Monday"}, "end": {"default": "23:59"}}},
#    "on": { "args": ["seconds"], "params": { "seconds": {"optional": True}}},
#    "off": {}
# }
async def handle_set(set_list_conf, obj, hash, args, argsh):
    return await _handle_command("set", set_list_conf, obj, hash, args, argsh)


async def handle_get(get_list_conf, obj, hash, args, argsh):
    return await _handle_command("get", get_list_conf, obj, hash, args, argsh)


def _command_options(command_config):
    options = []
    for command, config in command_config.items():
        if "options" in config:
            suffix = ":" + config["options"]
        elif "args" in config or "argsh" in config:
            suffix = ""
        else:
            suffix = ":noArg"
        options.append(command + suffix)
    return "Unknown argument ?, choose one of " + " ".join(options)


def _command_params(cmd_def, args, argsh):
    # Each invocation owns its parameters; do not store values in the config.
    params = dict(argsh) if "argsh" in cmd_def else {}
    params.update(zip(cmd_def.get("args", []), args[2:]))
    for name, config in cmd_def.get("params", {}).items():
        if name not in params:
            if "value" in config or "default" in config:
                params[name] = config.get("value", config.get("default"))
            elif not config.get("optional", False):
                return f"Required argument {name} missing."
        if name in params:
            params[name] = convert2format(params[name], config)
    return params


async def _handle_command(command_type, command_config, obj, hash, args, argsh):
    if len(args) < 2 or (not argsh and args[1] == "?"):
        return _command_options(command_config)

    cmd = args[1]
    if cmd not in command_config:
        return "Command not available for this device."
    cmd_def = command_config[cmd]
    positional = cmd_def.get("args", [])
    if len(args) > 2 and (
        args[2] == "?" or ("args" in cmd_def and len(args) - 2 > len(positional))
    ):
        return f"Usage: {command_type} {hash['NAME']} {cmd} " + " ".join(positional)
    if len(args) - 2 > len(positional):
        return f"Too many parameters provided: {args[2 + len(positional)]}"

    params = _command_params(cmd_def, args, argsh)
    if isinstance(params, str):
        return params
    if "function_param" in cmd_def:
        params["function_param"] = cmd_def["function_param"]
    function = cmd_def.get("function", command_type + "_" + cmd)
    if "function" in cmd_def:
        params["cmd"] = cmd
    return await getattr(obj, function)(hash, params)
