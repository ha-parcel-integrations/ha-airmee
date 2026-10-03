"""Config flow for the Airmee parcel tracker integration.

The setup flow opens on a menu choosing the source: an **account** (phone-OTP
login, parcels discovered from the inbox) or **tracking** (tracking-link tokens
plus the recipient's ``phone_number_hash``). ``temp_token`` and
``otp_hash_code`` exist only in the running flow and are never persisted.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .account.client import (
    AirmeeAccountError,
    AirmeeAccountOtpError,
    async_send_otp,
    async_verify_otp,
)
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_COUNTRY_CODE,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_OTP_CODE,
    CONF_PARCELS,
    CONF_PHONE_NUMBER,
    CONF_PHONE_NUMBER_HASH,
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DOMAIN,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)

_LOGGER = logging.getLogger(__name__)

_TRACKING_UNIQUE_ID = SOURCE_TRACKING


def normalize_tracking_code(value: str) -> str:
    """Return the tracking-link token trimmed of surrounding whitespace.

    Tokens are opaque and case-sensitive, so nothing else is touched.
    """
    return (value or "").strip()


def valid_tracking_code(value: str) -> bool:
    """Accept every non-empty token.

    Airmee's tracker accepts opaque tokens; a bad one simply comes back empty
    on the next poll. Do not add a format regex here.
    """
    return bool(value)


def _current_parcels(entry: ConfigEntry) -> list[dict[str, str]]:
    """Return a mutable copy of the tracked parcels list."""
    return [dict(item) for item in entry.options.get(CONF_PARCELS, [])]


def _clean_tracking_codes(values: list[str] | None) -> list[str]:
    """Normalise, drop blanks, and de-duplicate tracking codes."""
    codes: list[str] = []
    for value in values or []:
        code = normalize_tracking_code(value)
        if code and code not in codes:
            codes.append(code)
    return codes


def _account_unique_id(country_code: str, phone_number: str) -> str:
    """Return a stable id for the account without storing the phone number.

    The same account is typed many ways: ``+46``/``0046``/``46`` and a national
    number with or without its trunk zero. Hashing the raw form would hand the
    user a ``wrong_account`` abort at reauth and lock them out of their own
    entry, so the id is built from a normalised form. The API still receives
    exactly what was typed.
    """
    code = re.sub(r"\D", "", country_code.removeprefix("+").removeprefix("00"))
    number = re.sub(r"\D", "", phone_number).lstrip("0")
    digest = hashlib.sha256(f"{code}:{number}".encode()).hexdigest()
    return f"{SOURCE_ACCOUNT}:{digest[:16]}"


def _default_options() -> dict[str, Any]:
    """Options every new entry starts with."""
    return {
        CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
        CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
        CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
    }


_HASH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PHONE_NUMBER_HASH): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        )
    }
)
_PHONE_SCHEMA = vol.Schema(
    {vol.Required(CONF_COUNTRY_CODE): str, vol.Required(CONF_PHONE_NUMBER): str}
)
_OTP_SCHEMA = vol.Schema({vol.Required(CONF_OTP_CODE): str})


class AirmeeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI-driven configuration flow for the Airmee integration."""

    VERSION = 1

    def __init__(self) -> None:
        """Hold the in-progress OTP login; nothing here is ever persisted."""
        self._country_code = ""
        self._phone_number = ""
        self._temp_token = ""
        self._otp_hash_code = ""

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> AirmeeOptionsFlowHandler:
        """Return the options flow handler."""
        return AirmeeOptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose between the account inbox and tracking links."""
        return self.async_show_menu(
            step_id="user", menu_options=[SOURCE_ACCOUNT, SOURCE_TRACKING]
        )

    # -- tracking source -------------------------------------------------

    async def async_step_tracking(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the tracking hub, asking for the recipient's hash.

        One tracking hub exists per instance; the hash is that recipient's.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            phone_hash = normalize_tracking_code(user_input[CONF_PHONE_NUMBER_HASH])
            if not phone_hash:
                errors["base"] = "invalid_phone_number_hash"
            else:
                await self.async_set_unique_id(_TRACKING_UNIQUE_ID)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Airmee tracking",
                    data={
                        CONF_SOURCE: SOURCE_TRACKING,
                        CONF_PHONE_NUMBER_HASH: phone_hash,
                    },
                    options={CONF_PARCELS: [], **_default_options()},
                )
        return self.async_show_form(
            step_id=SOURCE_TRACKING, data_schema=_HASH_SCHEMA, errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace the stored hash (tracking) or redo the OTP login (account)."""
        entry = (
            self._get_reauth_entry()
            if self.source == SOURCE_REAUTH
            else self._get_reconfigure_entry()
        )
        if entry.data.get(CONF_SOURCE, SOURCE_TRACKING) == SOURCE_ACCOUNT:
            return await self.async_step_account()
        errors: dict[str, str] = {}
        if user_input is not None:
            phone_hash = normalize_tracking_code(user_input[CONF_PHONE_NUMBER_HASH])
            if not phone_hash:
                errors["base"] = "invalid_phone_number_hash"
            else:
                return self.async_update_reload_and_abort(
                    entry, data={**entry.data, CONF_PHONE_NUMBER_HASH: phone_hash}
                )
        return self.async_show_form(
            step_id="reconfigure", data_schema=_HASH_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Start re-authentication after Airmee rejected the stored credentials."""
        return await self.async_step_reconfigure()

    # -- account source ----------------------------------------------------

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the phone number and have Airmee text a code."""
        errors: dict[str, str] = {}
        if user_input is not None:
            country_code = str(user_input[CONF_COUNTRY_CODE]).strip()
            phone_number = str(user_input[CONF_PHONE_NUMBER]).strip()
            if not country_code or not phone_number:
                errors["base"] = "invalid_phone_number"
            else:
                try:
                    temp_token, otp_hash_code = await async_send_otp(
                        async_get_clientsession(self.hass), country_code, phone_number
                    )
                except AirmeeAccountOtpError:
                    errors["base"] = "invalid_phone_number"
                except (AirmeeAccountError, aiohttp.ClientError, TimeoutError):
                    errors["base"] = "cannot_connect"
                else:
                    self._country_code = country_code
                    self._phone_number = phone_number
                    self._temp_token = temp_token
                    self._otp_hash_code = otp_hash_code
                    return await self.async_step_otp()
        return self.async_show_form(
            step_id=SOURCE_ACCOUNT, data_schema=_PHONE_SCHEMA, errors=errors
        )

    async def async_step_otp(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Exchange the SMS code for the token pair and create/update the entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            otp_code = str(user_input[CONF_OTP_CODE]).strip()
            if not otp_code:
                errors["base"] = "invalid_otp"
            else:
                try:
                    tokens = await async_verify_otp(
                        async_get_clientsession(self.hass),
                        country_code=self._country_code,
                        phone_number=self._phone_number,
                        otp_code=otp_code,
                        otp_hash_code=self._otp_hash_code,
                        temp_token=self._temp_token,
                    )
                except AirmeeAccountOtpError:
                    errors["base"] = "invalid_otp"
                except (AirmeeAccountError, aiohttp.ClientError, TimeoutError):
                    errors["base"] = "cannot_connect"
                else:
                    return await self._finish_account(tokens)
        return self.async_show_form(
            step_id="otp", data_schema=_OTP_SCHEMA, errors=errors
        )

    async def _finish_account(self, tokens: dict[str, str]) -> ConfigFlowResult:
        """Persist the token pair only (never the phone number or temp token)."""
        data = {
            CONF_SOURCE: SOURCE_ACCOUNT,
            CONF_ACCESS_TOKEN: tokens[CONF_ACCESS_TOKEN],
            CONF_REFRESH_TOKEN: tokens[CONF_REFRESH_TOKEN],
        }
        unique_id = _account_unique_id(self._country_code, self._phone_number)
        self._temp_token = self._otp_hash_code = ""
        if self.source in (SOURCE_REAUTH, "reconfigure"):
            entry = (
                self._get_reauth_entry()
                if self.source == SOURCE_REAUTH
                else self._get_reconfigure_entry()
            )
            if entry.unique_id != unique_id:
                return self.async_abort(reason="wrong_account")
            return self.async_update_reload_and_abort(entry, data=data)
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Airmee account", data=data, options=_default_options()
        )


class AirmeeOptionsFlowHandler(OptionsFlow):
    """Manage tracked parcels separately from integration settings.

    An account entry has no tracked-parcel list: its menu is ``settings`` only.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer parcel management separately from integration settings."""
        menu_options = ["settings"]
        if self.config_entry.data.get(CONF_SOURCE, SOURCE_TRACKING) == SOURCE_TRACKING:
            menu_options.insert(0, "parcels")
        return self.async_show_menu(step_id="init", menu_options=menu_options)

    async def async_step_parcels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the complete tracked-code list."""
        if user_input is not None:
            codes = _clean_tracking_codes(user_input.get("tracking_codes"))
            return self.async_create_entry(
                title="",
                data={
                    **self.config_entry.options,
                    CONF_PARCELS: [{CONF_TRACKING_CODE: code} for code in codes],
                },
            )
        current_codes = [
            p[CONF_TRACKING_CODE] for p in _current_parcels(self.config_entry)
        ]
        schema = vol.Schema(
            {
                vol.Optional("tracking_codes"): selector.TextSelector(
                    selector.TextSelectorConfig(multiple=True)
                )
            }
        )
        return self.async_show_form(
            step_id="parcels",
            data_schema=self.add_suggested_values_to_schema(
                schema, {"tracking_codes": current_codes}
            ),
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the non-parcel integration settings."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    **self.config_entry.options,
                    CONF_DELIVERED_FILTER_TYPE: user_input[CONF_DELIVERED_FILTER_TYPE],
                    CONF_DELIVERED_FILTER_AMOUNT: int(
                        user_input[CONF_DELIVERED_FILTER_AMOUNT]
                    ),
                    CONF_INCLUDE_HISTORY: bool(user_input[CONF_INCLUDE_HISTORY]),
                },
            )
        current = self.config_entry.options
        schema: dict[Any, Any] = {
            vol.Required(
                CONF_DELIVERED_FILTER_TYPE,
                default=current.get(
                    CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
                ),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["days", "parcels"],
                    translation_key=CONF_DELIVERED_FILTER_TYPE,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Required(
                CONF_DELIVERED_FILTER_AMOUNT,
                default=current.get(
                    CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=365, step=1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(
                CONF_INCLUDE_HISTORY,
                default=current.get(CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY),
            ): selector.BooleanSelector(),
        }
        return self.async_show_form(step_id="settings", data_schema=vol.Schema(schema))
