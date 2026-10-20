"""Create whatsapp template."""

# Copyright (c) 2022, Shridhar Patil and contributors
# For license information, please see license.txt
import json
from urllib.parse import urlencode

import frappe
import magic
import requests
from frappe import _
from frappe.model.document import Document
from frappe.integrations.utils import make_post_request, make_request

from frappe_whatsapp.utils import get_whatsapp_account
from frappe_whatsapp.utils.template_parameters import (
	FORMAT_NAME,
	build_component_example,
	extract_named_parameter_names,
	extract_positional_parameter_numbers,
	from_api_parameter_format,
	get_component_variables,
	normalize_button_sample,
	reconcile_variable_rows,
	to_api_parameter_format,
)

class WhatsAppTemplates(Document):  # nosemgrep: frappe-modifying-but-not-committing-other-method -- get_settings() sets self._token/_url/_version/_business_id/_app_id/_headers as in-memory scratch for the outbound Meta HTTP call; they are not DocType fields and must not be persisted
    """Create whatsapp template."""

    def validate(self):
        if self.flags.skip_meta_sync:
            return

        self.set_whatsapp_account()
        if not self.language_code or self.has_value_changed("language"):
            lang_code = frappe.db.get_value("Language", self.language) or "en"
            self.language_code = lang_code.replace("-", "_")

        self.validate_template_type()
        reconcile_variable_rows(self)
        self.apply_legacy_variable_metadata()

        if self.header_type in ["IMAGE", "DOCUMENT"] and self.sample:
            self.get_session_id(self.sample)
            self.get_media_id(self.sample)

        if not self.is_new():
            self.update_template()

    def apply_legacy_variable_metadata(self):
        samples = [
            value.strip() for value in (self.sample_values or "").split(",") if value
        ]
        field_names = [
            value.strip() for value in (self.field_names or "").split(",") if value
        ]
        for index, variable in enumerate(
            get_component_variables(self, "BODY")
        ):
            if not variable.sample_value and index < len(samples):
                variable.sample_value = samples[index]
            if not variable.field_name and index < len(field_names):
                variable.field_name = field_names[index]

    def validate_template_type(self):
        allowed = {
            "MARKETING": {"Default", "Catalog", "Call Permission Request"},
            "UTILITY": {"Default", "Call Permission Request"},
            "AUTHENTICATION": {"One-Time Passcode"},
        }
        if self.category not in allowed:
            frappe.throw(_("Category must be MARKETING, UTILITY, or AUTHENTICATION."))
        if self.template_type not in allowed[self.category]:
            frappe.throw(
                _("{0} is not valid for category {1}.").format(
                    self.template_type, self.category
                )
            )
        if self.template_type != "Default":
            frappe.throw(
                _(
                    "{0} templates are synchronized from Meta as read-only in this phase."
                ).format(self.template_type)
            )

    def set_whatsapp_account(self):
        """Set whatsapp account to default if missing"""
        if not self.whatsapp_account:
            default_whatsapp_account = get_whatsapp_account()
            if not default_whatsapp_account:
                frappe.throw(
                    _(
                        "Please set a default outgoing WhatsApp Account or select an available WhatsApp Account"
                    )
                )
            else:
                self.whatsapp_account = default_whatsapp_account.name

    def get_session_id(self, file):
        """Upload media."""
        self.get_settings()

        # Check if it's a remote file, load data accordingly
        if file.startswith(('http://', 'https://')):
            remote_file_data = self._prepare_remote_file(file)
            file_type = remote_file_data['file_type']
            file_length = remote_file_data['file_size']
        else:
            file_content = self._read_local_file(file)
            mime = magic.Magic(mime=True)
            file_type = mime.from_buffer(file_content)
            file_length = len(file_content)

        payload = {
            'file_length': file_length,
            'file_type': file_type,
            'messaging_product': 'whatsapp'
        }

        response = make_post_request(
            f"{self._url}/{self._version}/{self._app_id}/uploads",
            headers=self._headers,
            data=json.loads(json.dumps(payload))
        )
        self._session_id = response['id']

    def _prepare_remote_file(self, file_url):
        """Download and return remote file content from URL."""
        try:
            response = requests.get(file_url, timeout=30)
            response.raise_for_status()
            
            file_content = response.content
            file_size = len(file_content)
            
            # Get MIME type from Content-Type header or detect from content
            content_type = response.headers.get('Content-Type', '').split(';')[0].strip()
            if content_type:
                file_type = content_type
            else:
                # Fallback to magic detection from content
                mime = magic.Magic(mime=True)
                file_type = mime.from_buffer(file_content)
            
            return {
                'file_content': file_content,
                'file_size': file_size,
                'file_type': file_type
            }
        except Exception as e:
            frappe.throw(f"Failed to download file from URL: {str(e)}")

    def get_media_id(self, file):
        self.get_settings()

        headers = {
                "authorization": f"OAuth {self._token}"
            }
        
        # Check if it's a remote file, load data accordingly
        if file.startswith(('http://', 'https://')):
            remote_file_data = self._prepare_remote_file(file)
            file_content = remote_file_data['file_content']
        else:
            file_content = self._read_local_file(file)

        payload = file_content
        response = make_post_request(
            f"{self._url}/{self._version}/{self._session_id}",
            headers=headers,
            data=payload
        )

        self._media_id = response['h']

    def _read_local_file(self, file_url):
        # Routed through File so path resolution stays inside Frappe's
        # vetted file handling — never feed a raw URL to open().
        return frappe.get_doc("File", {"file_url": file_url}).get_content()


    def _build_body_component(self):
        body = {
            "type": "BODY",
            "text": self.template,
        }
        example = build_component_example(
            "BODY",
            get_component_variables(self, "BODY"),
            self.parameter_format,
        )
        if example:
            body["example"] = example
        return body

    def _build_button_component(self):
        """Build the BUTTONS component, or None when the template has no buttons."""
        if not self.buttons:
            return None

        button_block = {"type": "BUTTONS", "buttons": []}
        for idx, btn in enumerate(self.buttons):
            b = {"type": btn.button_type, "text": btn.button_label}

            if btn.button_type == "Visit Website":
                b["type"] = "URL"
                b["url"] = btn.website_url
                if btn.url_type == "Dynamic":
                    variables = get_component_variables(self, "BUTTON", idx)
                    if len(variables) != 1 or not variables[0].sample_value:
                        frappe.throw(
                            _("Dynamic URL button {0} requires one sample value.").format(
                                idx + 1
                            )
                        )
                    b["example"] = [variables[0].sample_value]
            elif btn.button_type == "Call Phone":
                b["type"] = "PHONE_NUMBER"
                b["phone_number"] = btn.phone_number
            elif btn.button_type == "Quick Reply":
                b["type"] = "QUICK_REPLY"
            elif btn.button_type == "Multi-Product Message":
                b["type"] = "MPM"
            elif btn.button_type == "Catalog":
                b["type"] = "CATALOG"
            elif btn.button_type == "Flow":
                b["type"] = "FLOW"
                flow_id = (btn.flow_id or "").strip()
                if not flow_id:
                    frappe.throw(
                        _("Flow button {0} requires Flow ID.").format(idx + 1)
                    )
                b["flow_id"] = flow_id
                b["flow_action"] = "navigate"
                navigate_screen = (btn.navigate_screen or "").strip()
                if navigate_screen:
                    b["navigate_screen"] = navigate_screen

            button_block["buttons"].append(b)

        return button_block

    def _build_components(self):
        """Assemble Meta template components."""
        components = [self._build_body_component()]

        if self.header_type:
            components.append(self.get_header())

        if self.footer:
            components.append({"type": "FOOTER", "text": self.footer})

        button_block = self._build_button_component()
        if button_block:
            components.append(button_block)

        return components

    def after_insert(self):  # nosemgrep: frappe-modifying-but-not-committing -- self.actual_name/id/status are persisted via self.db_update() after the Meta round-trip; the static check can't trace through the API call
        if self.flags.skip_meta_sync:
            return

        # actual_name / id / status are persisted via self.db_update() below
        # after the Meta round-trip; the static check can't trace that call.
        if self.template_name:
            self.actual_name = self.template_name.lower().replace(" ", "_")  # nosemgrep: frappe-modifying-but-not-committing

        self.get_settings()
        components = self._build_components()
        data = {
            "name": self.actual_name,
            "language": self.language_code,
            "category": self.category,
            "parameter_format": to_api_parameter_format(self.parameter_format),
            "components": components,
        }

        try:
            response = make_post_request(
                f"{self._url}/{self._version}/{self._business_id}/message_templates",
                headers=self._headers,
                data=json.dumps(data),
            )
            self.id = response["id"]  # nosemgrep: frappe-modifying-but-not-committing
            self.status = response["status"]  # nosemgrep: frappe-modifying-but-not-committing
            self.db_update()
        except Exception as e:
            res = frappe.flags.integration_request.json().get("error", {})
            error_message = res.get("error_user_msg", res.get("message"))
            frappe.throw(
                msg=error_message,
                title=res.get("error_user_title", "Error"),
            )

    def update_template(self):
        """Update template to meta."""
        self.get_settings()
        components = self._build_components()
        data = {
            "category": self.category,
            "components": components,
            "parameter_format": to_api_parameter_format(self.parameter_format),
        }

        try:
            # post template to meta for update
            make_post_request(
                f"{self._url}/{self._version}/{self.id}",
                headers=self._headers,
                data=json.dumps(data),
            )
        except Exception as e:
            raise e
            # res = frappe.flags.integration_request.json()['error']
            # frappe.throw(
            #     msg=res.get('error_user_msg', res.get("message")),
            #     title=res.get("error_user_title", "Error"),
            # )

    def get_settings(self):
        """Get whatsapp settings."""
        # Underscore-prefixed attributes below are in-memory scratch for the
        # outbound HTTP call — they are not DocType fields and must not be
        # persisted. Semgrep's static check can't tell the difference.
        settings = frappe.get_doc("WhatsApp Account", self.whatsapp_account)
        self._token = settings.get_password("token")  # nosemgrep: frappe-modifying-but-not-committing-other-method
        self._url = settings.url  # nosemgrep: frappe-modifying-but-not-committing-other-method
        self._version = settings.version  # nosemgrep: frappe-modifying-but-not-committing-other-method
        self._business_id = settings.business_id  # nosemgrep: frappe-modifying-but-not-committing-other-method
        self._app_id = settings.app_id  # nosemgrep: frappe-modifying-but-not-committing-other-method

        self._headers = {  # nosemgrep: frappe-modifying-but-not-committing-other-method
            "authorization": f"Bearer {self._token}",
            "content-type": "application/json",
        }

    def on_trash(self):
        if self.meta_managed:
            frappe.throw(
                _("Delete this specialized template in Meta, then synchronize again.")
            )
        self.get_settings()
        url = f"{self._url}/{self._version}/{self._business_id}/message_templates?name={self.actual_name}"
        try:
            make_request("DELETE", url, headers=self._headers)
        except Exception:
            res = frappe.flags.integration_request.json().get("error", {})
            if res.get("error_user_title") == "Message Template Not Found":
                frappe.msgprint(
                    "Deleted locally", res.get("error_user_title", "Error"), alert=True
                )
            else:
                frappe.throw(
                    msg=res.get("error_user_msg"),
                    title=res.get("error_user_title", "Error"),
                )

    def get_header(self):
        """Get header format."""
        header = {"type": "HEADER", "format": self.header_type}
        if self.header_type == "TEXT":
            header["text"] = self.header
            variables = get_component_variables(self, "HEADER")
            example = build_component_example(
                "HEADER",
                variables,
                self.parameter_format,
            )
            if example:
                header["example"] = example
            elif self.sample:
                names = extract_named_parameter_names(self.header)
                header["example"] = (
                    {
                        "header_text_named_params": [
                            {"param_name": names[0], "example": self.sample}
                        ]
                    }
                    if names
                    else {"header_text": [self.sample]}
                )
        else:
            if not self.sample:
                frappe.throw(_("A media sample is required for {0} headers.").format(self.header_type))
            header.update({"example": {"header_handle": [self._media_id]}})

        return header

@frappe.whitelist()
def fetch():
    """Synchronize all template pages from every active WhatsApp account."""
    accounts = frappe.get_all(
        "WhatsApp Account",
        filters={"status": "Active"},
        fields=["name", "url", "version", "business_id"],
    )
    synced = 0

    try:
        for account in accounts:
            token = frappe.get_doc("WhatsApp Account", account.name).get_password("token")
            headers = {
                "authorization": f"Bearer {token}",
                "content-type": "application/json",
            }
            for template in _iter_meta_templates(account, headers):
                _sync_meta_template(account, template)
                synced += 1
    except Exception as exc:
        _throw_meta_error(exc)

    return _("Successfully fetched {0} template(s) from Meta").format(synced)


def _iter_meta_templates(account, headers):
    fields = "id,name,status,language,category,parameter_format,components"
    endpoint = (
        f"{account.url}/{account.version}/{account.business_id}/message_templates"
    )
    after = None

    while True:
        params = {"fields": fields, "limit": 100}
        if after:
            params["after"] = after
        response = make_request(
            "GET", f"{endpoint}?{urlencode(params)}", headers=headers
        )
        yield from response.get("data", [])
        paging = response.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after")
        if not paging.get("next") or not after:
            break


def _sync_meta_template(account, template):
    doc = _find_meta_template(account.name, template)
    is_new = doc.is_new()
    components = template.get("components") or []
    component_texts = []
    for component in components:
        component_texts.append(component.get("text", ""))
        component_texts.extend(
            button.get("url", "") for button in component.get("buttons", [])
        )

    doc.template_name = template["name"]
    doc.actual_name = template["name"]
    doc.status = template.get("status")
    doc.language_code = template.get("language")
    doc.language = _get_language(doc.language_code)
    doc.category = template.get("category")
    doc.id = template.get("id")
    doc.whatsapp_account = account.name
    doc.parameter_format = from_api_parameter_format(
        template.get("parameter_format"), component_texts
    )
    doc.template_type = _detect_template_type(doc.category, components)
    doc.meta_managed = int(doc.template_type != "Default")
    doc.raw_components = json.dumps(components, indent=2)
    doc.template = ""
    doc.header_type = ""
    doc.header = ""
    doc.footer = ""
    doc.set("buttons", [])
    doc.set("variables", [])

    _apply_meta_components(doc, components)
    doc.flags.skip_meta_sync = True
    if is_new:
        doc.name = _make_local_template_name(doc)
    doc.save(ignore_permissions=True)


def _find_meta_template(account_name, template):
    if template.get("id"):
        name = frappe.db.exists("WhatsApp Templates", {"id": template["id"]})
        if name:
            return frappe.get_doc("WhatsApp Templates", name)

    filters = {
        "whatsapp_account": account_name,
        "actual_name": template["name"],
        "language_code": template.get("language"),
    }
    name = frappe.db.exists("WhatsApp Templates", filters)
    return (
        frappe.get_doc("WhatsApp Templates", name)
        if name
        else frappe.new_doc("WhatsApp Templates")
    )


def _make_local_template_name(doc):
    base = f"{doc.template_name}-{doc.language_code}"
    if not frappe.db.exists("WhatsApp Templates", base):
        return base
    account_slug = frappe.scrub(doc.whatsapp_account).replace("_", "-")
    return f"{base}-{account_slug}"


def _get_language(language_code):
    normalized = (language_code or "").replace("_", "-")
    return (
        frappe.db.get_value("Language", {"language_code": normalized}, "name")
        or frappe.db.get_value("Language", {"language_code": language_code}, "name")
        or frappe.db.get_value("Language", {"language_code": "en"}, "name")
    )


def _detect_template_type(category, components):
    component_types = {
        component.get("type", "").upper() for component in components
    }
    button_types = {
        button.get("type", "").upper()
        for component in components
        if component.get("type", "").upper() == "BUTTONS"
        for button in component.get("buttons", [])
    }
    if category == "AUTHENTICATION" or "OTP" in button_types:
        return "One-Time Passcode"
    if "CALL_PERMISSION_REQUEST" in component_types:
        return "Call Permission Request"
    if "CATALOG" in button_types:
        return "Catalog"
    return "Default"


def _apply_meta_components(doc, components):
    type_map = {
        "URL": "Visit Website",
        "PHONE_NUMBER": "Call Phone",
        "QUICK_REPLY": "Quick Reply",
        "FLOW": "Flow",
        "MPM": "Multi-Product Message",
        "CATALOG": "Catalog",
    }

    for component in components:
        component_type = component.get("type", "").upper()
        if component_type == "BODY":
            doc.template = component.get("text", "")
            _append_example_variables(
                doc,
                "BODY",
                component.get("text", ""),
                component.get("example") or {},
            )
        elif component_type == "HEADER":
            doc.header_type = component.get("format", "")
            doc.header = component.get("text", "")
            if doc.header_type == "TEXT":
                _append_example_variables(
                    doc,
                    "HEADER",
                    component.get("text", ""),
                    component.get("example") or {},
                )
        elif component_type == "FOOTER":
            doc.footer = component.get("text", "")
        elif component_type == "BUTTONS":
            for button_index, button in enumerate(component.get("buttons") or []):
                raw_type = button.get("type", "").upper()
                if raw_type not in type_map:
                    continue
                values = {
                    "button_type": type_map[raw_type],
                    "button_label": button.get("text") or type_map[raw_type],
                }
                if raw_type == "URL":
                    values["website_url"] = button.get("url")
                    values["url_type"] = (
                        "Dynamic" if "{{" in (button.get("url") or "") else "Static"
                    )
                    examples = button.get("example") or []
                    values["example_url"] = ",".join(examples)
                elif raw_type == "PHONE_NUMBER":
                    values["phone_number"] = button.get("phone_number")
                elif raw_type == "FLOW":
                    values["flow_id"] = str(
                        button.get("flow_id") or button.get("flowId") or ""
                    )
                    values["navigate_screen"] = button.get("navigate_screen") or button.get(
                        "navigateScreen"
                    )
                doc.append("buttons", values)
                if raw_type == "URL" and values["url_type"] == "Dynamic":
                    _append_button_variable(doc, button_index, button)


def _append_example_variables(doc, component, text, example):
    if doc.parameter_format == FORMAT_NAME:
        key = (
            "body_text_named_params"
            if component == "BODY"
            else "header_text_named_params"
        )
        samples = {
            item.get("param_name"): item.get("example")
            for item in (example.get(key) or [])
        }
        for position, name in enumerate(
            extract_named_parameter_names(text), start=1
        ):
            doc.append(
                "variables",
                {
                    "component": component,
                    "position": position,
                    "parameter_name": name,
                    "sample_value": samples.get(name),
                },
            )
        return

    key = "body_text" if component == "BODY" else "header_text"
    values = example.get(key) or []
    if component == "BODY" and values and isinstance(values[0], list):
        values = values[0]
    positions = extract_positional_parameter_numbers(text)
    for position, _key in enumerate(positions, start=1):
        doc.append(
            "variables",
            {
                "component": component,
                "position": position,
                "parameter_name": str(position),
                "sample_value": values[position - 1]
                if position <= len(values)
                else "",
            },
        )


def _append_button_variable(doc, button_index, button):
    names = extract_named_parameter_names(button.get("url"))
    positions = extract_positional_parameter_numbers(button.get("url"))
    examples = button.get("example") or []
    keys = names or positions
    for position, key in enumerate(keys, start=1):
        raw_example = examples[position - 1] if position <= len(examples) else ""
        doc.append(
            "variables",
            {
                "component": "BUTTON",
                "button_index": button_index,
                "position": position,
                "parameter_name": str(key),
                "sample_value": normalize_button_sample(
                    button.get("url"), raw_example
                ),
            },
        )


def _throw_meta_error(exc):
    integration_request = getattr(frappe.flags, "integration_request", None)
    if integration_request and hasattr(integration_request, "json"):
        try:
            error = integration_request.json().get("error", {})
        except (ValueError, json.JSONDecodeError):
            error = {}
        if error:
            frappe.throw(
                error.get("error_user_msg") or error.get("message") or str(exc),
                title=error.get("error_user_title") or _("Meta API Error"),
            )
    frappe.throw(_("Unable to fetch templates from Meta: {0}").format(exc))


def upsert_doc_without_hooks(doc, child_dt=None, child_field=None):
    """Backward-compatible wrapper used by older integrations and tests."""
    doc.flags.skip_meta_sync = True
    doc.save(ignore_permissions=True)
    