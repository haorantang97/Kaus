"""Read one native credential without asking for new Keychain permission.

The small child process keeps legacy Keychain interaction flags out of the host
process. Neither the request nor a diagnostic contains a credential.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

if __name__ != '__main__':
    from drivers.account_quota import QuotaUnavailable


async def read_password(service: str, account: str) -> str | None:
    if sys.platform != 'darwin':
        return None
    if not all(isinstance(value, str) and value and '\0' not in value and len(value) < 1024
               for value in (service, account)):
        raise QuotaUnavailable(reason='credential_access_required')
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(Path(__file__).resolve()),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(
            process.communicate(json.dumps([service, account]).encode()), timeout=4,
        )
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode == 3:
        return None
    if process.returncode or len(stdout) > 1024 * 1024:
        raise QuotaUnavailable(reason='credential_access_required')
    try:
        return stdout.decode('utf-8')
    except UnicodeDecodeError:
        raise QuotaUnavailable(reason='credential_access_required') from None


def _read_native(service, account):
    # All APIs here are reads. In particular, do not use the native Agent's
    # store constructor: some implementations probe it with set/get/delete.
    import ctypes as c
    cf = c.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    sec = c.CDLL('/System/Library/Frameworks/Security.framework/Security')
    pointer = c.c_void_p
    cf.CFStringCreateWithCString.argtypes = [pointer, c.c_char_p, c.c_uint32]
    cf.CFStringCreateWithCString.restype = pointer
    cf.CFDictionaryCreate.argtypes = [pointer, pointer, pointer, c.c_ssize_t, pointer, pointer]
    cf.CFDictionaryCreate.restype = pointer
    cf.CFDataGetLength.argtypes = [pointer]
    cf.CFDataGetLength.restype = c.c_ssize_t
    cf.CFDataGetBytePtr.argtypes = [pointer]
    cf.CFDataGetBytePtr.restype = pointer
    cf.CFRelease.argtypes = [pointer]
    sec.SecKeychainSetUserInteractionAllowed.argtypes = [c.c_ubyte]
    sec.SecKeychainSetUserInteractionAllowed.restype = c.c_int32
    sec.SecItemCopyMatching.argtypes = [pointer, c.POINTER(pointer)]
    sec.SecItemCopyMatching.restype = c.c_int32
    if sec.SecKeychainSetUserInteractionAllowed(False) != 0:
        return 4, b''
    symbol = lambda lib, name: pointer.in_dll(lib, name).value
    owned = []
    result = pointer()
    try:
        for value in (service, account):
            ref = cf.CFStringCreateWithCString(None, value.encode(), 0x08000100)
            if not ref:
                return 4, b''
            owned.append(ref)
        entries = [
            (symbol(sec, 'kSecClass'), symbol(sec, 'kSecClassGenericPassword')),
            (symbol(sec, 'kSecAttrService'), owned[0]),
            (symbol(sec, 'kSecAttrAccount'), owned[1]),
            (symbol(sec, 'kSecMatchLimit'), symbol(sec, 'kSecMatchLimitOne')),
            (symbol(sec, 'kSecReturnData'), symbol(cf, 'kCFBooleanTrue')),
            (symbol(sec, 'kSecUseAuthenticationUI'), symbol(sec, 'kSecUseAuthenticationUIFail')),
        ]
        keys = (pointer * len(entries))(*(key for key, _ in entries))
        values = (pointer * len(entries))(*(value for _, value in entries))
        # References stay alive until the query returns, so no retain callbacks
        # are needed in this short-lived dictionary.
        query = cf.CFDictionaryCreate(None, keys, values, len(entries), None, None)
        if not query:
            return 4, b''
        owned.append(query)
        status = sec.SecItemCopyMatching(query, c.byref(result))
        if status == -25300:
            return 3, b''
        if status != 0 or not result:
            return 4, b''
        size = cf.CFDataGetLength(result)
        if not 0 <= size <= 1024 * 1024:
            return 4, b''
        return 0, c.string_at(cf.CFDataGetBytePtr(result), size)
    finally:
        if result:
            cf.CFRelease(result)
        for ref in reversed(owned):
            cf.CFRelease(ref)


if __name__ == '__main__':
    try:
        args = json.loads(sys.stdin.buffer.read(4096))
        status, secret = _read_native(*args)
        if not status:
            sys.stdout.buffer.write(secret)
        sys.exit(status)
    except Exception:
        sys.exit(4)
