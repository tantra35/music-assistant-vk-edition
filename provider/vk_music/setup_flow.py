"""Interactive VK ID authorization flow for Music Assistant."""
from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.models.setup_flow import SetupFlowError
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from .vendor.vkpymusic import Service
from .vendor.vkpymusic.vk_api import VkApiException, VkApiRequestBuilder, make_request_async

from . import CONF_TOKEN, CONF_USER_AGENT
from .auth import (
    VKAuthError,
    build_authorize_url,
    exchange_auth_code,
    extract_auth_code,
    make_pkce,
)

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

# VK Music Android app surface — the UA that serves the catalog family.
USER_AGENT = "VKMusicAndroidApp"

_AUTH_STEP_ID = "auth"
_REDIRECT_KEY = "redirect"


async def _probe_catalog_access(service: Service) -> bool:
    """Check the token actually opens catalog.getAudio.

    Legacy oauth.vk.com tokens validate fine against audio methods but are
    denied the whole catalog family — reject them here with a clear error.
    """
    request = VkApiRequestBuilder.build_req_get_catalog_audio(need_blocks=0)
    service.fill_user_agent_and_token(request)
    try:
        response = await make_request_async(request)
    except VkApiException:
        return False
    data = response.data
    return isinstance(data, dict) and "catalog" in data


def _build_auth_form(auth_url: str) -> list[ConfigEntry]:
    """The single form: a clickable login link plus the paste-back field.

    LABEL entries render through the frontend's MarkdownText (links are real
    <a href>), which is the closest a 2.10.9 setup form gets to an Open
    button — session.external() cannot complete because VK never calls back.
    """
    instructions = (
        f"[**Открыть страницу входа VK ID**]({auth_url})\n\n"
        "1. Войдите как обычно (пароль, код из MAX и т.п.).\n"
        "2. После входа браузер окажется на странице "
        "`api.vk.ru/blank.html#payload=...` — она может выглядеть пустой.\n"
        "3. Скопируйте ПОЛНЫЙ адрес из адресной строки.\n"
        "4. Вставьте его в поле ниже.\n\n"
        "Альтернатива: если у вас уже есть catalog-токен VK ID "
        "(например, из vkpymusic/config_vk.ini), вставьте его вместо адреса."
    )
    link_entry = ConfigEntry(
        key="auth_link",
        type=ConfigEntryType.LABEL,
        # NOTE: the pinned frontend renders LABEL entries from `label`
        # (displayLabel() = label ?? key) — default_value is never shown.
        label=instructions,
        required=False,
    )
    redirect_entry = ConfigEntry(
        key=_REDIRECT_KEY,
        type=ConfigEntryType.SECURE_STRING,
        label=(
            "Шаг 2. Адрес после входа — api.vk.ru/blank.html#payload=... "
            "(или готовый vk1... токен)"
        ),
        required=True,
    )
    return [link_entry, redirect_entry]


async def run_setup(session: SetupSession) -> None:
    """Drive the VK ID OAuth2 login and save only the resulting token."""
    errors: dict[str, str] | None = None
    # PKCE verifier is generated here and never leaves the host; only its
    # challenge is embedded into the login URL.
    code_verifier, code_challenge = make_pkce()
    auth_url = build_authorize_url(code_challenge)

    while True:
        values = await session.form(
            _build_auth_form(auth_url),
            step_id=_AUTH_STEP_ID,
            errors=errors,
            last_step=False,
        )
        errors = None
        pasted = str(values.get(_REDIRECT_KEY) or "").strip()

        try:
            if pasted.startswith("vk1"):
                # A ready catalog-capable token pasted directly.
                token = pasted
            else:
                code, device_id, redirect_base = extract_auth_code(pasted)
                data = await exchange_auth_code(
                    code, code_verifier, device_id, redirect_base
                )
                token = str(data["access_token"])
        except VKAuthError as err:
            errors = {"base": str(err)}
            continue

        service = Service(user_agent=USER_AGENT, token=token)
        try:
            token_valid = await service.is_token_valid_async()
        except Exception:
            token_valid = False
        if not token_valid:
            errors = {"base": "VK не принял этот токен. Проверьте и вставьте заново."}
            continue

        if not await _probe_catalog_access(service):
            errors = {
                "base": (
                    "Токен получен, но каталог VK (catalog.getAudio) им не "
                    "открывается — это устаревший тип токена. Пройдите вход "
                    "по ссылке заново."
                )
            }
            continue

        try:
            # Only the access token and client User-Agent are persisted.
            await session.finish(
                {
                    CONF_TOKEN: token,
                    CONF_USER_AGENT: USER_AGENT,
                }
            )
            return
        except SetupFlowError as err:
            errors = {"base": str(err)}
