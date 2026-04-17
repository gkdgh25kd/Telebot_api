from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
import traceback
from typing import Any

from flask import Flask, jsonify, request
from telethon.errors import ApiIdInvalidError, PasswordHashInvalidError, PhoneCodeEmptyError, PhoneCodeExpiredError, PhoneCodeInvalidError, PhoneNumberBannedError, PhoneNumberInvalidError, SessionPasswordNeededError
from telethon.errors.rpcerrorlist import FloodWaitError
from telethon.sessions import StringSession
from telethon.sync import TelegramClient


BASE_DIR = Path(__file__).resolve().parent
SESSION_FILE = BASE_DIR / '.telebot_telegram_session.json'
LOG_FILE = BASE_DIR / 'telegram-helper.log'
HELPER_PORT = 43123

app = Flask(__name__)

pending_auth: dict[str, Any] = {
    'phone_number': None,
    'phone_code_hash': None,
    'api_id': None,
    'api_hash': None,
    'temp_session_string': None,
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec='seconds')


def mask_phone(phone_number: str | None) -> str:
    raw = str(phone_number or '').strip()
    if len(raw) <= 4:
        return raw
    return f'{raw[:2]}***{raw[-2:]}'


def log_event(event: str, payload: dict[str, Any] | None = None) -> None:
    body = payload or {}
    line = f"[{now_iso()}] {event} | {json.dumps(body, ensure_ascii=False)}"
    print(line)
    try:
        with LOG_FILE.open('a', encoding='utf-8') as file_handle:
            file_handle.write(line + '\n')
    except Exception:
        pass


def normalize_phone_number(value: Any) -> str:
    raw = str(value or '').strip().replace(' ', '').replace('-', '').replace('(', '').replace(')', '')
    if not raw:
        raise ValueError('número é obrigatório')
    if raw.startswith('+'):
        return raw
    digits = ''.join(ch for ch in raw if ch.isdigit())
    if not digits:
        raise ValueError('número inválido')
    return f'+{digits}'


def describe_sent_code(sent_code: Any) -> dict[str, Any]:
    code_type_obj = getattr(sent_code, 'type', None)
    next_type_obj = getattr(sent_code, 'next_type', None)
    return {
        'type': code_type_obj.__class__.__name__ if code_type_obj else None,
        'next_type': next_type_obj.__class__.__name__ if next_type_obj else None,
        'timeout_seconds': getattr(sent_code, 'timeout', None),
        'is_password_required': bool(getattr(sent_code, 'type', None).__class__.__name__ == 'SentCodeTypeSetUpEmailRequired'),
    }


def log_exception(context: str, error: Exception) -> None:
    log_event(context, {
        'error_type': error.__class__.__name__,
        'error_message': str(error),
        'traceback': traceback.format_exc(),
    })


def json_response(payload: dict[str, Any], status_code: int = 200):
    response = jsonify(payload)
    response.status_code = status_code
    return response


@app.after_request
def apply_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'GET,POST,OPTIONS'
    return response


@app.route('/health', methods=['GET', 'OPTIONS'])
def healthcheck():
    if request.method == 'OPTIONS':
        return ('', 204)
    return json_response({'success': True, 'status': 'ok', 'port': HELPER_PORT, 'log_file': str(LOG_FILE)})


def load_saved_session() -> dict[str, Any] | None:
    if not SESSION_FILE.exists():
        return None

    try:
        return json.loads(SESSION_FILE.read_text(encoding='utf-8'))
    except Exception:
        return None


def save_session_file(payload: dict[str, Any]) -> None:
    SESSION_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8')


def clear_pending_auth() -> None:
    pending_auth.update({
        'phone_number': None,
        'phone_code_hash': None,
        'api_id': None,
        'api_hash': None,
        'temp_session_string': None,
    })


def build_client(api_id: int, api_hash: str, session_string: str = '') -> TelegramClient:
    return TelegramClient(StringSession(session_string), api_id, api_hash)


def normalize_api_id(value: Any) -> int:
    try:
        return int(str(value).strip())
    except Exception as exc:
        raise ValueError('api_id inválido') from exc


def normalize_required_text(value: Any, field_name: str) -> str:
    text = str(value or '').strip()
    if not text:
        raise ValueError(f'{field_name} é obrigatório')
    return text


def normalize_identity(value: Any) -> str:
    return ''.join(ch for ch in str(value or '').strip().lower() if ch.isalnum())


def get_saved_api_credentials() -> tuple[int, str, str]:
    saved = load_saved_session()
    if not saved:
        raise ValueError('Nenhuma sessão salva encontrada. Conecte a conta Telegram API primeiro.')

    api_id = normalize_api_id(saved.get('api_id'))
    api_hash = normalize_required_text(saved.get('api_hash'), 'api_hash')
    session_string = str(saved.get('session_string') or '').strip()
    if not session_string:
        raise ValueError('Sessão salva inválida. Faça login novamente na conexão Telegram API.')

    return api_id, api_hash, session_string


def build_member_payload(user: Any) -> dict[str, Any]:
    first_name = str(getattr(user, 'first_name', '') or '').strip()
    last_name = str(getattr(user, 'last_name', '') or '').strip()
    full_name = f'{first_name} {last_name}'.strip()
    username = getattr(user, 'username', None)
    status_obj = getattr(user, 'status', None)

    return {
        'name': full_name or (f'@{username}' if username else str(getattr(user, 'id', 'Sem nome'))),
        'username': f'@{username}' if username else None,
        'status': status_obj.__class__.__name__ if status_obj else 'unknown',
        'peerId': str(getattr(user, 'id', '') or ''),
        'source': 'telegram-api',
        'extractedAt': datetime.utcnow().isoformat() + 'Z',
    }


def get_session_status_payload() -> dict[str, Any]:
    saved = load_saved_session()
    if not saved:
        return {
            'success': True,
            'helper': 'online',
            'connected': False,
            'authorized': False,
            'account': None,
        }

    api_id = normalize_api_id(saved.get('api_id'))
    api_hash = normalize_required_text(saved.get('api_hash'), 'api_hash')
    session_string = str(saved.get('session_string') or '')
    if not session_string:
        return {
            'success': True,
            'helper': 'online',
            'connected': False,
            'authorized': False,
            'account': None,
        }

    client = build_client(api_id, api_hash, session_string)
    try:
        client.connect()
        if not client.is_user_authorized():
            return {
                'success': True,
                'helper': 'online',
                'connected': False,
                'authorized': False,
                'account': None,
            }

        me = client.get_me()
        return {
            'success': True,
            'helper': 'online',
            'connected': True,
            'authorized': True,
            'account': {
                'id': getattr(me, 'id', None),
                'phone': getattr(me, 'phone', None),
                'username': getattr(me, 'username', None),
                'first_name': getattr(me, 'first_name', None),
                'last_name': getattr(me, 'last_name', None),
            },
        }
    finally:
        try:
            client.disconnect()
        except Exception:
            pass


@app.route('/session/status', methods=['GET', 'OPTIONS'])
def session_status():
    if request.method == 'OPTIONS':
        return ('', 204)

    try:
        return json_response(get_session_status_payload())
    except Exception as error:
        return json_response({
            'success': False,
            'helper': 'online',
            'error': f'Falha ao consultar sessão: {error}',
        }, 500)


@app.route('/auth/send-code', methods=['POST', 'OPTIONS'])
def send_code():
    if request.method == 'OPTIONS':
        return ('', 204)

    payload = request.get_json(silent=True) or {}
    log_event('auth.send_code.request', {
        'has_api_id': bool(payload.get('api_id')),
        'has_api_hash': bool(payload.get('api_hash')),
        'phone_input': mask_phone(payload.get('phone_number')),
    })

    try:
        api_id = normalize_api_id(payload.get('api_id'))
        api_hash = normalize_required_text(payload.get('api_hash'), 'api_hash')
        phone_number = normalize_phone_number(payload.get('phone_number'))

        clear_pending_auth()

        client = build_client(api_id, api_hash)
        try:
            client.connect()
            sent_code = client.send_code_request(phone_number)
            delivery = describe_sent_code(sent_code)
            temp_session_string = client.session.save()
        finally:
            try:
                client.disconnect()
            except Exception:
                pass

        pending_auth.update({
            'phone_number': phone_number,
            'phone_code_hash': sent_code.phone_code_hash,
            'api_id': api_id,
            'api_hash': api_hash,
            'temp_session_string': temp_session_string,
        })

        log_event('auth.send_code.success', {
            'phone_number': mask_phone(phone_number),
            'delivery': delivery,
            'phone_code_hash_prefix': str(sent_code.phone_code_hash or '')[:8],
        })

        return json_response({
            'success': True,
            'phone_number': phone_number,
            'phone_code_hash': sent_code.phone_code_hash,
            'delivery': delivery,
            'message': 'Código enviado com sucesso',
        })
    except ApiIdInvalidError:
        clear_pending_auth()
        log_event('auth.send_code.fail', {'error': 'api_id/api_hash inválidos'})
        return json_response({'success': False, 'error': 'api_id/api_hash inválidos'}, 400)
    except PhoneNumberInvalidError:
        clear_pending_auth()
        log_event('auth.send_code.fail', {'error': 'número inválido'})
        return json_response({'success': False, 'error': 'Número inválido. Use DDI+DDD+numero, ex: +5511999999999'}, 400)
    except PhoneNumberBannedError:
        clear_pending_auth()
        log_event('auth.send_code.fail', {'error': 'número banido'})
        return json_response({'success': False, 'error': 'Número banido pelo Telegram'}, 400)
    except FloodWaitError as error:
        clear_pending_auth()
        log_event('auth.send_code.fail', {'error': 'flood_wait', 'wait_seconds': getattr(error, 'seconds', None)})
        return json_response({'success': False, 'error': f'Telegram pediu espera antes de novo código ({getattr(error, "seconds", "?")}s)'}, 429)
    except Exception as error:
        clear_pending_auth()
        log_exception('auth.send_code.exception', error)
        return json_response({'success': False, 'error': str(error)}, 500)


@app.route('/auth/verify-code', methods=['POST', 'OPTIONS'])
def verify_code():
    if request.method == 'OPTIONS':
        return ('', 204)

    payload = request.get_json(silent=True) or {}
    log_event('auth.verify_code.request', {
        'has_api_id': bool(payload.get('api_id')),
        'has_api_hash': bool(payload.get('api_hash')),
        'phone_input': mask_phone(payload.get('phone_number')),
        'has_code': bool(payload.get('code')),
        'has_password': bool(payload.get('password')),
    })

    try:
        api_id = normalize_api_id(payload.get('api_id'))
        api_hash = normalize_required_text(payload.get('api_hash'), 'api_hash')
        phone_number = normalize_phone_number(payload.get('phone_number'))
        code = normalize_required_text(payload.get('code'), 'código')
        password = str(payload.get('password') or '').strip()

        temp_session_string = str(payload.get('temp_session_string') or pending_auth.get('temp_session_string') or '')
        phone_code_hash = str(payload.get('phone_code_hash') or pending_auth.get('phone_code_hash') or '').strip()
        client = build_client(api_id, api_hash, temp_session_string)
        client.connect()

        if not phone_code_hash:
            try:
                client.disconnect()
            except Exception:
                pass
            raise ValueError('phone_code_hash ausente. Envie o código novamente.')

        try:
            client.sign_in(phone=phone_number, code=code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            if not password:
                log_event('auth.verify_code.need_password', {
                    'phone_number': mask_phone(phone_number),
                })
                return json_response({
                    'success': False,
                    'requires_password': True,
                    'error': 'Esta conta exige senha 2FA',
                }, 400)
            client.sign_in(password=password)

        me = client.get_me()
        session_string = client.session.save()
        try:
            client.disconnect()
        except Exception:
            pass
        save_session_file({
            'api_id': api_id,
            'api_hash': api_hash,
            'phone_number': phone_number,
            'session_string': session_string,
            'account': {
                'id': getattr(me, 'id', None),
                'phone': getattr(me, 'phone', None),
                'username': getattr(me, 'username', None),
                'first_name': getattr(me, 'first_name', None),
                'last_name': getattr(me, 'last_name', None),
            },
        })

        pending_auth.update({
            'phone_number': None,
            'phone_code_hash': None,
            'api_id': None,
            'api_hash': None,
            'temp_session_string': None,
        })

        log_event('auth.verify_code.success', {
            'phone_number': mask_phone(phone_number),
            'user_id': getattr(me, 'id', None),
            'username': getattr(me, 'username', None),
        })

        return json_response({
            'success': True,
            'authorized': True,
            'account': {
                'id': getattr(me, 'id', None),
                'phone': getattr(me, 'phone', None),
                'username': getattr(me, 'username', None),
                'first_name': getattr(me, 'first_name', None),
                'last_name': getattr(me, 'last_name', None),
            },
            'message': 'Conta autenticada com sucesso',
        })
    except (PhoneCodeInvalidError, PhoneCodeExpiredError, PhoneCodeEmptyError):
        log_event('auth.verify_code.fail', {'error': 'código inválido/expirado'})
        return json_response({'success': False, 'error': 'Código inválido ou expirado'}, 400)
    except PasswordHashInvalidError:
        log_event('auth.verify_code.fail', {'error': 'senha 2FA inválida'})
        return json_response({'success': False, 'error': 'Senha 2FA inválida'}, 400)
    except ApiIdInvalidError:
        log_event('auth.verify_code.fail', {'error': 'api_id/api_hash inválidos'})
        return json_response({'success': False, 'error': 'api_id/api_hash inválidos'}, 400)
    except FloodWaitError as error:
        log_event('auth.verify_code.fail', {'error': 'flood_wait', 'wait_seconds': getattr(error, 'seconds', None)})
        return json_response({'success': False, 'error': f'Telegram pediu espera antes de nova tentativa ({getattr(error, "seconds", "?")}s)'}, 429)
    except Exception as error:
        log_exception('auth.verify_code.exception', error)
        return json_response({'success': False, 'error': str(error)}, 500)


@app.route('/api/list-groups', methods=['GET', 'OPTIONS'])
def api_list_groups():
    if request.method == 'OPTIONS':
        return ('', 204)

    try:
        api_id, api_hash, session_string = get_saved_api_credentials()
        client = build_client(api_id, api_hash, session_string)
        groups: list[dict[str, Any]] = []

        try:
            client.connect()
            if not client.is_user_authorized():
                return json_response({'success': False, 'error': 'Sessão não autorizada. Faça login novamente.'}, 401)

            for dialog in client.iter_dialogs():
                if not getattr(dialog, 'is_group', False):
                    continue

                entity = getattr(dialog, 'entity', None)
                title = str(getattr(dialog, 'name', '') or '').strip()
                if not title:
                    continue

                groups.append({
                    'groupId': str(getattr(dialog, 'id', '') or ''),
                    'title': title,
                    'normalizedTitle': normalize_identity(title),
                    'peerId': str(getattr(entity, 'id', '') or ''),
                    'username': str(getattr(entity, 'username', '') or '').strip() or None,
                })
        finally:
            try:
                client.disconnect()
            except Exception:
                pass

        groups.sort(key=lambda item: item['title'].lower())
        log_event('api.list_groups.success', {'count': len(groups)})
        return json_response({'success': True, 'count': len(groups), 'groups': groups})
    except Exception as error:
        log_exception('api.list_groups.exception', error)
        return json_response({'success': False, 'error': str(error)}, 500)


@app.route('/api/extract-members', methods=['POST', 'OPTIONS'])
def api_extract_members():
    if request.method == 'OPTIONS':
        return ('', 204)

    payload = request.get_json(silent=True) or {}
    group_title = str(payload.get('group_title') or '').strip()
    group_id_raw = str(payload.get('group_id') or '').strip()
    max_members_raw = payload.get('max_members', 0)

    try:
        max_members = int(max_members_raw or 0)
    except Exception:
        max_members = 0

    log_event('api.extract_members.request', {
        'group_title': group_title,
        'group_id': group_id_raw,
        'max_members': max_members,
    })

    try:
        if not group_title and not group_id_raw:
            raise ValueError('Informe group_title ou group_id para extrair membros.')

        api_id, api_hash, session_string = get_saved_api_credentials()
        client = build_client(api_id, api_hash, session_string)

        selected_dialog = None
        selected_entity = None

        try:
            client.connect()
            if not client.is_user_authorized():
                return json_response({'success': False, 'error': 'Sessão não autorizada. Faça login novamente.'}, 401)

            target_norm = normalize_identity(group_title) if group_title else ''
            target_group_id = group_id_raw

            fallback_match = None
            for dialog in client.iter_dialogs():
                if not getattr(dialog, 'is_group', False):
                    continue

                dialog_title = str(getattr(dialog, 'name', '') or '').strip()
                dialog_norm = normalize_identity(dialog_title)
                dialog_id = str(getattr(dialog, 'id', '') or '')

                by_id = bool(target_group_id and dialog_id == target_group_id)
                exact_by_title = bool(target_norm and dialog_norm == target_norm)
                contains_by_title = bool(target_norm and target_norm in dialog_norm)

                if by_id or exact_by_title:
                    selected_dialog = dialog
                    selected_entity = dialog.entity
                    break

                if contains_by_title and fallback_match is None:
                    fallback_match = dialog

            if not selected_dialog and fallback_match is not None:
                selected_dialog = fallback_match
                selected_entity = fallback_match.entity

            if not selected_dialog or not selected_entity:
                return json_response({'success': False, 'error': 'Grupo não encontrado na conta conectada via API.'}, 404)

            members = []
            for user in client.iter_participants(selected_entity):
                members.append(build_member_payload(user))
                if max_members > 0 and len(members) >= max_members:
                    break

            result = {
                'success': True,
                'membersCount': len(members),
                'members': members,
                'group': {
                    'groupId': str(getattr(selected_dialog, 'id', '') or ''),
                    'title': str(getattr(selected_dialog, 'name', '') or '').strip(),
                },
                'source': 'telegram-api',
            }
            log_event('api.extract_members.success', {
                'group_id': result['group']['groupId'],
                'group_title': result['group']['title'],
                'members_count': len(members),
            })
            return json_response(result)
        finally:
            try:
                client.disconnect()
            except Exception:
                pass
    except Exception as error:
        log_exception('api.extract_members.exception', error)
        return json_response({'success': False, 'error': str(error)}, 500)


@app.route('/debug/log-tail', methods=['GET', 'OPTIONS'])
def debug_log_tail():
    if request.method == 'OPTIONS':
        return ('', 204)

    try:
        limit = int(request.args.get('limit', '120'))
        limit = max(10, min(limit, 500))
    except Exception:
        limit = 120

    if not LOG_FILE.exists():
        return json_response({'success': True, 'lines': []})

    lines = LOG_FILE.read_text(encoding='utf-8', errors='ignore').splitlines()
    return json_response({'success': True, 'lines': lines[-limit:]})


@app.route('/session/disconnect', methods=['POST', 'OPTIONS'])
def disconnect_session():
    if request.method == 'OPTIONS':
        return ('', 204)

    try:
        saved = load_saved_session()
        if saved:
            client = build_client(normalize_api_id(saved.get('api_id')), normalize_required_text(saved.get('api_hash'), 'api_hash'), str(saved.get('session_string') or ''))
            client.connect()
            try:
                client.log_out()
            finally:
                client.disconnect()

        clear_pending_auth()

        if SESSION_FILE.exists():
            SESSION_FILE.unlink()

        return json_response({'success': True, 'message': 'Sessão removida com sucesso'})
    except Exception as error:
        clear_pending_auth()
        return json_response({'success': False, 'error': str(error)}, 500)


import os

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 43123))

    print(f'Helper Telegram API rodando na porta {port}')
    print('Instale dependências com: pip install Flask Telethon')

    app.run(
        host='0.0.0.0',  # MUITO IMPORTANTE
        port=port,
        debug=False,
        use_reloader=False,
        threaded=False
    )