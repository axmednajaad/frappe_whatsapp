# Copyright (c) 2025, Shridhar Patil and contributors
# For license information, please see license.txt

import json
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.integrations.utils import make_post_request, make_request


class WhatsAppFlow(Document):
    def before_save(self):
        """Generate flow JSON before saving."""
        self.flow_json = json.dumps(self.generate_flow_json(), indent=2)

    def validate(self):
        """Validate flow configuration."""
        self.validate_screens()
        self.validate_conditional_fields()

    def validate_screens(self):
        """Ensure at least one screen exists and has valid configuration."""
        if not self.screens:
            frappe.throw(_("At least one screen is required"))

        screen_ids = []
        terminal_count = 0

        for screen in self.screens:
            if screen.screen_id in screen_ids:
                frappe.throw(_("Duplicate screen ID: {0}").format(screen.screen_id))
            screen_ids.append(screen.screen_id)

            if screen.terminal:
                terminal_count += 1

        if terminal_count == 0:
            frappe.throw(_("At least one screen must be marked as terminal"))

    def validate_conditional_fields(self):
        """Meta requires Footer in both If branches when Footer is used inside If."""
        by_screen_condition = {}
        for field in self.fields or []:
            if not field.enabled:
                continue
            condition = (field.visible_condition or "").strip()
            if not condition:
                continue
            key = (field.screen, condition)
            by_screen_condition.setdefault(key, {"Then": [], "Else": []})
            branch = field.condition_branch or "Then"
            if branch not in ("Then", "Else"):
                branch = "Then"
            by_screen_condition[key][branch].append(field)

        for (screen_id, condition), branches in by_screen_condition.items():
            then_has_footer = any(f.field_type == "Footer" for f in branches["Then"])
            else_has_footer = any(f.field_type == "Footer" for f in branches["Else"])
            if then_has_footer != else_has_footer:
                frappe.throw(
                    _(
                        "Screen {0}: condition must include a Footer in both Then and Else "
                        "branches (Meta If rule). Condition: {1}"
                    ).format(screen_id, condition)
                )
            if then_has_footer and else_has_footer:
                outer_footer = any(
                    f.enabled
                    and f.screen == screen_id
                    and f.field_type == "Footer"
                    and not (f.visible_condition or "").strip()
                    for f in (self.fields or [])
                )
                if outer_footer:
                    frappe.throw(
                        _(
                            "Screen {0}: cannot mix an unconditional Footer with Footers "
                            "inside an If condition."
                        ).format(screen_id)
                    )

            # TextArea must not use max-chars (Meta rejection)
            for branch_fields in branches.values():
                for field in branch_fields:
                    if field.field_type == "TextArea" and field.max_chars:
                        frappe.throw(
                            _(
                                "Field {0}: Meta does not allow max-chars on TextArea. "
                                "Clear Max Characters."
                            ).format(field.field_name)
                        )

        for field in self.fields or []:
            if field.field_type == "TextArea" and field.max_chars:
                frappe.throw(
                    _(
                        "Field {0}: Meta does not allow max-chars on TextArea. "
                        "Clear Max Characters."
                    ).format(field.field_name)
                )

    def generate_flow_json(self):
        """Generate WhatsApp Flow JSON from DocType configuration.

        Generates a client-only flow (no endpoint required).
        Data is collected and returned via webhook when flow completes.
        """
        flow = {
            "version": self.data_api_version or "6.0",
            "screens": []
        }

        # Build a map of which fields are passed to each screen from previous screens
        screen_incoming_data = self.build_screen_data_map()

        for screen in self.screens:
            screen_data = self.build_screen(screen, screen_incoming_data.get(screen.screen_id, {}))
            flow["screens"].append(screen_data)

        # Note: We intentionally do NOT include 'routing_model' or 'data_api_version'
        # This creates a client-only flow that doesn't require an endpoint
        # Data is returned via webhook (nfm_reply) when the flow completes

        return flow

    def build_screen_data_map(self):
        """Build a map of fields passed to each screen from previous screens.

        Returns:
            dict: {screen_id: {field_name: {"type": "string", "__example__": ""}}}
        """
        screen_data_map = {}
        accumulated_fields = {}  # Fields accumulated from all previous screens

        for i, screen in enumerate(self.screens):
            # Current screen receives all accumulated fields from previous screens
            if accumulated_fields:
                screen_data_map[screen.screen_id] = accumulated_fields.copy()

            # Add this screen's input fields to accumulated fields for next screens
            for field in self.fields:
                if field.screen != screen.screen_id:
                    continue
                if not field.enabled:
                    continue
                # Only input fields (not display components) are passed in payload
                if field.field_type not in [
                    "TextHeading", "TextSubheading", "TextBody",
                    "TextCaption", "Image", "EmbeddedLink", "Footer"
                ]:
                    accumulated_fields[field.field_name] = {
                        "type": "string",
                        "__example__": ""
                    }

        return screen_data_map

    def build_screen(self, screen, incoming_data=None):
        """Build a single screen definition."""
        screen_data = {
            "id": screen.screen_id,
            "title": screen.screen_title,
            "data": incoming_data or {},
            "layout": {
                "type": "SingleColumnLayout",
                "children": []
            }
        }

        if screen.terminal:
            screen_data["terminal"] = True
            screen_data["success"] = True

        if screen.refresh_on_back:
            screen_data["refresh_on_back"] = True

        # Build fields for this screen
        children = self.build_screen_fields(screen)
        screen_data["layout"]["children"] = children

        return screen_data

    def build_screen_fields(self, screen):
        """Build field components for a screen, including Meta If / Switch grouping."""
        children = []
        has_footer = False
        unconditional_fields = []
        conditional_order = []
        conditional_groups = {}
        switch_order = []
        switch_groups = {}

        for field in self.fields:
            if field.screen != screen.screen_id:
                continue
            if not field.enabled:
                continue

            condition = (field.visible_condition or "").strip()
            switch_on = (getattr(field, "switch_on", None) or "").strip()
            switch_case = (getattr(field, "switch_case", None) or "").strip()

            if condition:
                if condition not in conditional_groups:
                    conditional_groups[condition] = {"Then": [], "Else": []}
                    conditional_order.append(condition)
                branch = field.condition_branch or "Then"
                if branch not in ("Then", "Else"):
                    branch = "Then"
                conditional_groups[condition][branch].append(field)
            elif switch_on:
                if not switch_case:
                    frappe.throw(
                        _("Field {0}: Switch Case is required when Switch On is set.").format(
                            field.field_name
                        )
                    )
                if switch_on not in switch_groups:
                    switch_groups[switch_on] = {}
                    switch_order.append(switch_on)
                for case_key in [c.strip() for c in switch_case.split(",") if c.strip()]:
                    switch_groups[switch_on].setdefault(case_key, []).append(field)
            else:
                unconditional_fields.append(field)

        for field in unconditional_fields:
            component = self.build_field_component(field, screen)
            if component:
                children.append(component)
                if field.field_type == "Footer":
                    has_footer = True

        for condition in conditional_order:
            branches = conditional_groups[condition]
            then_components = []
            else_components = []

            for field in branches["Then"]:
                component = self.build_field_component(field, screen)
                if component:
                    then_components.append(component)
                    if field.field_type == "Footer":
                        has_footer = True

            for field in branches["Else"]:
                component = self.build_field_component(field, screen)
                if component:
                    else_components.append(component)
                    if field.field_type == "Footer":
                        has_footer = True

            if not then_components and not else_components:
                continue

            if_component = {
                "type": "If",
                "condition": condition,
                "then": then_components or [{"type": "TextCaption", "text": " "}],
            }
            if else_components or any(c.get("type") == "Footer" for c in then_components):
                if_component["else"] = else_components
            children.append(if_component)

        for switch_on in switch_order:
            cases = {}
            for case_key, fields in switch_groups[switch_on].items():
                case_components = []
                for field in fields:
                    component = self.build_field_component(field, screen)
                    if component:
                        case_components.append(component)
                        if field.field_type == "Footer":
                            has_footer = True
                cases[case_key] = case_components

            # Meta: Footer in one case requires Footer in every case
            cases_with_footer = {
                key for key, comps in cases.items() if any(c.get("type") == "Footer" for c in comps)
            }
            if cases_with_footer and cases_with_footer != set(cases.keys()):
                missing = ", ".join(sorted(set(cases.keys()) - cases_with_footer))
                frappe.throw(
                    _(
                        "Screen {0}: Switch on {1} has Footer in some cases but not in: {2}"
                    ).format(screen.screen_id, switch_on, missing)
                )

            children.append({
                "type": "Switch",
                "value": switch_on,
                "cases": cases,
            })

        if not has_footer:
            footer_action = self.build_footer_action(None, screen)
            children.append({
                "type": "Footer",
                "label": "Continue" if not screen.terminal else "Complete",
                "on-click-action": footer_action
            })

        return children

    def build_field_component(self, field, screen):
        """Build a single field component."""
        field_type = field.field_type

        # Text display components (no input)
        if field_type in ["TextHeading", "TextSubheading", "TextBody", "TextCaption"]:
            return {
                "type": field_type,
                "text": field.label or ""
            }

        # Image component
        if field_type == "Image":
            component = {
                "type": "Image",
                "src": field.init_value or ""
            }
            if field.label:
                component["alt-text"] = field.label
            return component

        # Embedded link
        if field_type == "EmbeddedLink":
            return {
                "type": "EmbeddedLink",
                "text": field.label or "Link",
                "on-click-action": {
                    "name": "navigate",
                    "next": {"type": "screen", "name": field.init_value or ""}
                }
            }

        # Footer (submit button)
        if field_type == "Footer":
            action = self.build_footer_action(field, screen)
            return {
                "type": "Footer",
                "label": field.label or "Submit",
                "on-click-action": action
            }

        # Input components
        component = {
            "type": field_type,
            "name": field.field_name,
            "label": field.label or field.field_name
        }

        if field.required:
            component["required"] = True

        if field.helper_text:
            component["helper-text"] = field.helper_text

        if field.init_value:
            component["init-value"] = field.init_value

        # TextInput / TextArea validation
        if field_type in ["TextInput", "TextArea"]:
            if field.min_chars:
                component["min-chars"] = field.min_chars
            # Meta allows max-chars on TextInput only
            if field_type == "TextInput" and field.max_chars:
                component["max-chars"] = field.max_chars
            if field.error_message:
                component["error-message"] = field.error_message

        # Options for dropdown/radio/checkbox
        if field_type in ["Dropdown", "RadioButtonsGroup", "CheckboxGroup"]:
            options = self.parse_options(field.options)
            if options:
                component["data-source"] = options

        # OptIn specific
        if field_type == "OptIn":
            component["label"] = field.label or "I agree"
            if field.required:
                component["required"] = True

        return component

    def build_footer_action(self, field, screen):
        """Build the action for a footer button."""
        payload = self.build_payload(screen, footer_field=field)

        if screen.terminal:
            return {
                "name": "complete",
                "payload": payload
            }

        next_screen = self.get_next_screen(screen)
        if next_screen:
            return {
                "name": "navigate",
                "next": {
                    "type": "screen",
                    "name": next_screen.screen_id
                },
                "payload": payload
            }

        return {
            "name": "complete",
            "payload": payload
        }

    def build_payload(self, screen, footer_field=None):
        """Build payload with field values up to and including this screen.

        If footer_field.payload_fields is set (comma-separated names), only those
        current-screen form fields are included (plus prior-screen data refs).
        """
        payload = {}
        allowed_current = None
        if footer_field and getattr(footer_field, "payload_fields", None):
            allowed_current = {
                name.strip()
                for name in str(footer_field.payload_fields).split(",")
                if name.strip()
            }

        non_input_types = {
            "TextHeading", "TextSubheading", "TextBody",
            "TextCaption", "Image", "EmbeddedLink", "Footer"
        }

        for s in self.screens:
            if s.screen_id == screen.screen_id:
                for field in self.fields:
                    if field.screen != s.screen_id:
                        continue
                    if not field.enabled or field.field_type in non_input_types:
                        continue
                    if allowed_current is not None and field.field_name not in allowed_current:
                        continue
                    # Skip fields that only appear in Else/Then other branch when
                    # footer specifies payload — already handled by allowed_current.
                    # When auto payload: skip fields that are conditional-only on
                    # the opposite branch of this footer.
                    if (
                        allowed_current is None
                        and (field.visible_condition or "").strip()
                        and footer_field
                        and (footer_field.visible_condition or "").strip()
                        and field.visible_condition.strip() == footer_field.visible_condition.strip()
                        and (field.condition_branch or "Then") != (footer_field.condition_branch or "Then")
                    ):
                        continue
                    if (
                        allowed_current is None
                        and (field.visible_condition or "").strip()
                        and footer_field
                        and (footer_field.visible_condition or "").strip()
                        and field.visible_condition.strip() != footer_field.visible_condition.strip()
                    ):
                        continue
                    if (
                        allowed_current is None
                        and (field.visible_condition or "").strip()
                        and footer_field
                        and not (footer_field.visible_condition or "").strip()
                    ):
                        # Unconditional footer should not reference conditional-only fields
                        continue
                    payload[field.field_name] = "${form." + field.field_name + "}"
                break

            for field in self.fields:
                if field.screen != s.screen_id:
                    continue
                if not field.enabled or field.field_type in non_input_types:
                    continue
                payload[field.field_name] = "${data." + field.field_name + "}"

        return payload

    def get_next_screen(self, current_screen):
        """Get the next screen in sequence."""
        found_current = False
        for screen in self.screens:
            if found_current:
                return screen
            if screen.screen_id == current_screen.screen_id:
                found_current = True
        return None

    def parse_options(self, options_json):
        """Parse options JSON string to list."""
        if not options_json:
            return []
        try:
            options = json.loads(options_json)
            if isinstance(options, list):
                return options
            return []
        except json.JSONDecodeError:
            return []

    @frappe.whitelist()
    def create_on_whatsapp(self, meta_flow_name: str | None = None):
        """Create the flow on WhatsApp Business API.

        Args:
                meta_flow_name: Optional unique name for Meta WABA.
                        Defaults to this document's flow_name.
        """
        if self.flow_id:
            frappe.throw(_("Flow already exists on WhatsApp. Use update instead."))

        account = frappe.get_doc("WhatsApp Account", self.whatsapp_account)
        token = account.get_password("token")

        # Create the flow
        url = f"{account.url}/{account.version}/{account.business_id}/flows"
        create_name = (meta_flow_name or self.flow_name or "").strip()
        if not create_name:
            frappe.throw(_("Flow name is required"))

        payload = {
            "name": create_name,
            "categories": [self.category]
        }

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        try:
            import requests
            response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=60)
            data = {}
            try:
                data = response.json()
            except Exception:
                data = {"raw": response.text}

            if response.status_code != 200:
                err = data.get("error") or {}
                error_msg = err.get("error_user_msg") or err.get("message") or response.text
                frappe.throw(_("Failed to create flow: {0}").format(error_msg))

            self.flow_id = data.get("id")
            self.status = "Draft"
            self.save()

            # Upload the flow JSON
            self.upload_flow_json()

            frappe.msgprint(_("Flow created successfully on WhatsApp"))

        except frappe.ValidationError:
            raise
        except Exception as e:
            frappe.throw(_("Failed to create flow: {0}").format(str(e)))

    @frappe.whitelist()
    def upload_flow_json(self):
        """Upload flow JSON to WhatsApp."""
        if not self.flow_id:
            frappe.throw(_("Flow must be created on WhatsApp first"))

        account = frappe.get_doc("WhatsApp Account", self.whatsapp_account)
        token = account.get_password("token")

        url = f"{account.url}/{account.version}/{self.flow_id}/assets"

        # Generate fresh flow JSON
        flow_json = self.generate_flow_json()

        headers = {
            "Authorization": f"Bearer {token}"
        }

        files = {
            "file": ("flow.json", json.dumps(flow_json), "application/json"),
            "name": (None, "flow.json"),
            "asset_type": (None, "FLOW_JSON")
        }

        try:
            import requests
            response = requests.post(url, headers=headers, files=files, timeout=60)

            data = {}
            try:
                data = response.json()
            except Exception:
                data = {"raw": response.text}

            if response.status_code != 200:
                err = data.get("error") or {}
                error_msg = err.get("error_user_msg") or err.get("message") or response.text
                validation_errors = data.get("validation_errors") or []
                if validation_errors:
                    details = "; ".join(
                        ve.get("message") or str(ve) for ve in validation_errors[:5]
                    )
                    error_msg = f"{error_msg} — {details}"
                frappe.throw(_("Failed to upload flow JSON: {0}").format(error_msg))

            # Meta may return 200 with validation_errors that block publish
            validation_errors = data.get("validation_errors") or []
            if validation_errors:
                details = "; ".join(
                    ve.get("message") or str(ve) for ve in validation_errors[:5]
                )
                frappe.throw(_("Flow JSON has validation errors: {0}").format(details))

            # Updating a published flow returns it to Draft
            if self.status == "Published":
                self.db_set("status", "Draft", update_modified=True)

            frappe.msgprint(_("Flow JSON uploaded successfully"))

        except frappe.ValidationError:
            raise
        except requests.exceptions.RequestException as e:
            frappe.throw(_("Failed to upload flow JSON: {0}").format(str(e)))

    @frappe.whitelist()
    def publish_flow(self):
        """Publish the flow to make it available for use."""
        if not self.flow_id:
            frappe.throw(_("Flow must be created on WhatsApp first"))

        if self.status == "Published":
            frappe.throw(_("Flow is already published"))

        account = frappe.get_doc("WhatsApp Account", self.whatsapp_account)
        token = account.get_password("token")

        url = f"{account.url}/{account.version}/{self.flow_id}/publish"

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        try:
            import requests
            response = requests.post(url, headers=headers, timeout=60)

            data = {}
            try:
                data = response.json()
            except Exception:
                data = {"raw": response.text}

            if response.status_code != 200:
                err = data.get("error") or {}
                error_msg = err.get("error_user_msg") or err.get("message") or response.text
                frappe.throw(_("Failed to publish flow: {0}").format(error_msg))

            self.status = "Published"
            self.save()

            frappe.msgprint(_("Flow published successfully"))

        except frappe.ValidationError:
            raise
        except Exception as e:
            frappe.throw(_("Failed to publish flow: {0}").format(str(e)))

    @frappe.whitelist()
    def update_and_publish(self):
        """Upload current Flow JSON to the existing Meta flow_id and publish.

        Preferred for editable Flows (returns Published → Draft → Published).
        """
        if not self.flow_id:
            frappe.throw(_("Flow must be created on WhatsApp first"))

        self.upload_flow_json()
        self.reload()
        if self.status != "Published":
            self.publish_flow()
        return {"flow_id": self.flow_id, "status": self.status}

    @frappe.whitelist()
    def recreate_and_publish(self):
        """Update in place when possible; otherwise create a uniquely named Meta flow.

        Causes of previous failures:
        - Deprecate 400 when already deprecated
        - Create 400 when flow name is not unique in the WABA
        """
        # 1) Prefer in-place update (keeps same flow_id for sending)
        if self.flow_id:
            try:
                return self.update_and_publish()
            except Exception:
                frappe.log_error(
                    title=f"WhatsApp Flow in-place update failed: {self.name}",
                    message=frappe.get_traceback(),
                )

        # 2) Soft-deprecate old id (ignore already-deprecated)
        old_flow_id = self.flow_id
        if old_flow_id:
            try:
                self.deprecate_flow()
            except Exception as e:
                msg = str(e)
                if "already deprecated" not in msg.lower() and "139003" not in msg:
                    frappe.log_error(
                        title=f"WhatsApp Flow deprecate failed: {self.name}",
                        message=frappe.get_traceback(),
                    )

        # 3) Create with a unique Meta name (WABA names stay reserved after deprecate)
        unique_meta_name = f"{self.flow_name} {frappe.utils.now_datetime().strftime('%Y%m%d%H%M%S')}"
        self.flow_id = None
        self.status = "Draft"
        self.save(ignore_permissions=True)

        self.create_on_whatsapp(meta_flow_name=unique_meta_name)
        self.reload()
        if self.status != "Published":
            self.publish_flow()

        frappe.msgprint(
            _("Flow published. Meta name: {0}. Flow ID: {1}").format(
                unique_meta_name, self.flow_id
            )
        )
        return {
            "flow_id": self.flow_id,
            "status": self.status,
            "meta_flow_name": unique_meta_name,
            "previous_flow_id": old_flow_id,
        }

    @frappe.whitelist()
    def deprecate_flow(self):
        """Deprecate the flow."""
        if not self.flow_id:
            frappe.throw(_("Flow must be created on WhatsApp first"))

        account = frappe.get_doc("WhatsApp Account", self.whatsapp_account)
        token = account.get_password("token")

        url = f"{account.url}/{account.version}/{self.flow_id}/deprecate"

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        try:
            import requests
            response = requests.post(url, headers=headers, timeout=60)
            data = {}
            try:
                data = response.json()
            except Exception:
                data = {"raw": response.text}

            if response.status_code != 200:
                err = data.get("error") or {}
                error_msg = err.get("error_user_msg") or err.get("message") or response.text
                # Already deprecated is a no-op success
                if "already deprecated" in str(error_msg).lower() or err.get("error_subcode") == 4016008:
                    self.status = "Deprecated"
                    self.save()
                    frappe.msgprint(_("Flow was already deprecated on WhatsApp"))
                    return
                frappe.throw(_("Failed to deprecate flow: {0}").format(error_msg))

            self.status = "Deprecated"
            self.save()

            frappe.msgprint(_("Flow deprecated successfully"))

        except frappe.ValidationError:
            raise
        except Exception as e:
            frappe.throw(_("Failed to deprecate flow: {0}").format(str(e)))

    @frappe.whitelist()
    def delete_from_whatsapp(self):
        """Delete the flow from WhatsApp."""
        if not self.flow_id:
            frappe.throw(_("Flow does not exist on WhatsApp"))

        account = frappe.get_doc("WhatsApp Account", self.whatsapp_account)
        token = account.get_password("token")

        url = f"{account.url}/{account.version}/{self.flow_id}"

        headers = {
            "Authorization": f"Bearer {token}"
        }

        try:
            import requests
            response = requests.delete(url, headers=headers)
            response.raise_for_status()

            self.flow_id = None
            self.status = "Draft"
            self.save()

            frappe.msgprint(_("Flow deleted from WhatsApp"))

        except Exception as e:
            frappe.throw(_("Failed to delete flow: {0}").format(str(e)))

    @frappe.whitelist()
    def get_flow_preview(self):
        """Get the preview URL for the flow."""
        if not self.flow_id:
            frappe.throw(_("Flow must be created on WhatsApp first"))

        account = frappe.get_doc("WhatsApp Account", self.whatsapp_account)
        token = account.get_password("token")

        url = f"{account.url}/{account.version}/{self.flow_id}?fields=preview.invalidate(false)"

        headers = {
            "Authorization": f"Bearer {token}"
        }

        try:
            import requests
            response = requests.get(url, headers=headers)
            response.raise_for_status()

            data = response.json()
            preview_url = data.get("preview", {}).get("preview_url")

            if preview_url:
                self.preview_url = preview_url
                self.save()
                return preview_url

            frappe.throw(_("Preview URL not available"))

        except Exception as e:
            frappe.throw(_("Failed to get preview: {0}").format(str(e)))

    @frappe.whitelist()
    def send_test(self, phone_number, message=None):
        """Send a test flow message to a phone number.

        Args:
            phone_number: Phone number to send to (must be a test number for draft flows)
            message: Optional message body
        """
        if not self.flow_id:
            frappe.throw(_("Flow must be created on WhatsApp first"))

        # Create a WhatsApp Message to send the flow
        msg = frappe.get_doc({
            "doctype": "WhatsApp Message",
            "type": "Outgoing",
            "to": phone_number,
            "content_type": "flow",
            "flow": self.name,
            "flow_cta": self.flow_cta or "Open Form",
            "message": message or f"Test: {self.flow_name}",
            "whatsapp_account": self.whatsapp_account
        })
        msg.insert(ignore_permissions=True)

        frappe.msgprint(
            _("Test flow sent to {0}").format(phone_number),
            indicator="green"
        )
        return msg.name

    @frappe.whitelist()
    def get_flow_status(self):
        """Get flow status and validation errors from WhatsApp."""
        if not self.flow_id:
            frappe.throw(_("Flow must be created on WhatsApp first"))

        account = frappe.get_doc("WhatsApp Account", self.whatsapp_account)
        token = account.get_password("token")

        url = f"{account.url}/{account.version}/{self.flow_id}?fields=id,name,status,categories,validation_errors,json_version,data_api_version,data_channel_uri,preview,whatsapp_business_account"

        headers = {
            "Authorization": f"Bearer {token}"
        }

        try:
            import requests
            response = requests.get(url, headers=headers)
            response.raise_for_status()

            data = response.json()

            # Update local status
            if data.get("status"):
                self.status = data["status"].title()
                self.save()

            # Check for validation errors
            validation_errors = data.get("validation_errors", [])
            if validation_errors:
                error_messages = []
                for error in validation_errors:
                    error_messages.append(f"- {error.get('error', 'Unknown error')} at {error.get('error_type', 'unknown')}")
                frappe.msgprint(
                    _("Flow Validation Errors:\n{0}").format("\n".join(error_messages)),
                    title=_("Validation Errors"),
                    indicator="red"
                )
            else:
                frappe.msgprint(
                    _("Flow Status: {0}\nJSON Version: {1}").format(
                        data.get("status", "Unknown"),
                        data.get("json_version", "Unknown")
                    ),
                    title=_("Flow Status"),
                    indicator="green" if data.get("status") == "PUBLISHED" else "blue"
                )

            return data

        except Exception as e:
            frappe.throw(_("Failed to get flow status: {0}").format(str(e)))

    @frappe.whitelist()
    def sync_from_whatsapp(self):
        """Sync flow details from WhatsApp."""
        if not self.flow_id:
            frappe.throw(_("Flow ID is required to sync"))

        account = frappe.get_doc("WhatsApp Account", self.whatsapp_account)
        token = account.get_password("token")

        # Get flow details
        url = f"{account.url}/{account.version}/{self.flow_id}?fields=id,name,status,categories,json_version,preview"

        headers = {
            "Authorization": f"Bearer {token}"
        }

        try:
            import requests
            response = requests.get(url, headers=headers)
            response.raise_for_status()

            data = response.json()

            # Update local fields
            if data.get("status"):
                self.status = data["status"].title()
            if data.get("categories"):
                self.category = data["categories"][0] if data["categories"] else "OTHER"
            if data.get("json_version"):
                self.data_api_version = data["json_version"]
            if data.get("preview", {}).get("preview_url"):
                self.preview_url = data["preview"]["preview_url"]

            # Try to get the flow JSON
            flow_json = self.fetch_flow_json()
            if flow_json:
                self.flow_json = json.dumps(flow_json, indent=2)

            self.save()
            frappe.msgprint(_("Flow synced successfully from WhatsApp"), indicator="green")

            return data

        except Exception as e:
            frappe.throw(_("Failed to sync flow: {0}").format(str(e)))

    def fetch_flow_json(self):
        """Fetch the flow JSON from WhatsApp."""
        if not self.flow_id:
            return None

        account = frappe.get_doc("WhatsApp Account", self.whatsapp_account)
        token = account.get_password("token")

        url = f"{account.url}/{account.version}/{self.flow_id}/assets"

        headers = {
            "Authorization": f"Bearer {token}"
        }

        try:
            import requests
            response = requests.get(url, headers=headers)
            response.raise_for_status()

            data = response.json()
            assets = data.get("data", [])

            # Find the flow.json asset
            for asset in assets:
                if asset.get("name") == "flow.json":
                    # Download the asset
                    download_url = asset.get("download_url")
                    if download_url:
                        asset_response = requests.get(download_url, headers=headers)
                        if asset_response.status_code == 200:
                            return asset_response.json()

            return None

        except Exception as e:
            frappe.log_error(f"Failed to fetch flow JSON: {str(e)}")
            return None


@frappe.whitelist()
def get_whatsapp_flows(whatsapp_account):
    """Get list of all flows from WhatsApp Business Account.

    Args:
        whatsapp_account: Name of WhatsApp Account document

    Returns:
        List of flows from WhatsApp
    """
    account = frappe.get_doc("WhatsApp Account", whatsapp_account)
    token = account.get_password("token")

    url = f"{account.url}/{account.version}/{account.business_id}/flows?fields=id,name,status,categories"

    headers = {
        "Authorization": f"Bearer {token}"
    }

    try:
        import requests
        response = requests.get(url, headers=headers)
        response.raise_for_status()

        data = response.json()
        flows = data.get("data", [])

        # Check which flows already exist locally
        for flow in flows:
            existing = frappe.db.exists("WhatsApp Flow", {"flow_id": flow["id"]})
            flow["exists_locally"] = bool(existing)
            flow["local_name"] = existing if existing else None

        return flows

    except Exception as e:
        frappe.throw(_("Failed to fetch flows: {0}").format(str(e)))


@frappe.whitelist()
def import_flow_from_whatsapp(whatsapp_account, flow_id, flow_name=None):
    """Import a flow from WhatsApp into Frappe.

    Args:
        whatsapp_account: Name of WhatsApp Account document
        flow_id: WhatsApp Flow ID to import
        flow_name: Optional name for the flow (uses WhatsApp name if not provided)

    Returns:
        Name of created WhatsApp Flow document
    """
    # Check if flow already exists
    existing = frappe.db.exists("WhatsApp Flow", {"flow_id": flow_id})
    if existing:
        frappe.throw(_("Flow already exists: {0}").format(existing))

    account = frappe.get_doc("WhatsApp Account", whatsapp_account)
    token = account.get_password("token")

    # Get flow details
    url = f"{account.url}/{account.version}/{flow_id}?fields=id,name,status,categories,json_version,preview"

    headers = {
        "Authorization": f"Bearer {token}"
    }

    try:
        import requests
        response = requests.get(url, headers=headers)
        response.raise_for_status()

        data = response.json()

        # Create the flow document
        flow_doc = frappe.get_doc({
            "doctype": "WhatsApp Flow",
            "flow_name": flow_name or data.get("name", f"Imported Flow {flow_id}"),
            "whatsapp_account": whatsapp_account,
            "flow_id": flow_id,
            "status": data.get("status", "Draft").title(),
            "category": data["categories"][0] if data.get("categories") else "OTHER",
            "data_api_version": data.get("json_version", "6.0"),
            "preview_url": data.get("preview", {}).get("preview_url", "")
        })

        # Try to fetch and parse the flow JSON to create screens/fields
        flow_json = fetch_flow_json_by_id(whatsapp_account, flow_id)
        if flow_json:
            flow_doc.flow_json = json.dumps(flow_json, indent=2)
            parse_flow_json_to_screens(flow_doc, flow_json)

        # Skip validation since we're importing (may not have screens)
        flow_doc.flags.ignore_validate = True
        flow_doc.insert(ignore_permissions=True)

        frappe.msgprint(_("Flow imported successfully: {0}").format(flow_doc.name), indicator="green")

        return flow_doc.name

    except Exception as e:
        frappe.throw(_("Failed to import flow: {0}").format(str(e)))


def fetch_flow_json_by_id(whatsapp_account, flow_id):
    """Fetch flow JSON by flow ID."""
    account = frappe.get_doc("WhatsApp Account", whatsapp_account)
    token = account.get_password("token")

    url = f"{account.url}/{account.version}/{flow_id}/assets"

    headers = {
        "Authorization": f"Bearer {token}"
    }

    try:
        import requests
        response = requests.get(url, headers=headers)
        response.raise_for_status()

        data = response.json()
        assets = data.get("data", [])

        for asset in assets:
            if asset.get("name") == "flow.json":
                download_url = asset.get("download_url")
                if download_url:
                    asset_response = requests.get(download_url, headers=headers)
                    if asset_response.status_code == 200:
                        return asset_response.json()

        return None

    except Exception as e:
        frappe.log_error(f"Failed to fetch flow JSON: {str(e)}")
        return None


@frappe.whitelist()
def sync_all_flows(whatsapp_account):
    """Sync all flows from WhatsApp Business Account.

    Imports new flows and updates existing ones.

    Args:
        whatsapp_account: Name of WhatsApp Account document

    Returns:
        Dict with counts: imported, updated, skipped
    """
    account = frappe.get_doc("WhatsApp Account", whatsapp_account)
    token = account.get_password("token")

    url = f"{account.url}/{account.version}/{account.business_id}/flows?fields=id,name,status,categories"

    headers = {
        "Authorization": f"Bearer {token}"
    }

    result = {"imported": 0, "updated": 0, "skipped": 0}

    try:
        import requests
        response = requests.get(url, headers=headers)
        response.raise_for_status()

        data = response.json()
        flows = data.get("data", [])

        for flow in flows:
            flow_id = flow.get("id")
            flow_name = flow.get("name")

            # Check if flow exists locally
            existing = frappe.db.exists("WhatsApp Flow", {"flow_id": flow_id})

            if existing:
                # Update existing flow
                try:
                    flow_doc = frappe.get_doc("WhatsApp Flow", existing)
                    flow_doc.status = flow.get("status", "Draft").title()
                    if flow.get("categories"):
                        flow_doc.category = flow["categories"][0]

                    # Try to fetch flow JSON and update screens/fields
                    flow_json = fetch_flow_json_by_id(whatsapp_account, flow_id)
                    if flow_json:
                        flow_doc.flow_json = json.dumps(flow_json, indent=2)
                        flow_doc.data_api_version = flow_json.get("version", "6.0")

                        # Clear existing screens and fields, then re-parse
                        flow_doc.screens = []
                        flow_doc.fields = []
                        parse_flow_json_to_screens(flow_doc, flow_json)

                    flow_doc.flags.ignore_validate = True
                    flow_doc.save(ignore_permissions=True)
                    result["updated"] += 1
                except Exception as e:
                    frappe.log_error(f"Failed to update flow {flow_id}: {str(e)}")
                    result["skipped"] += 1
            else:
                # Import new flow
                try:
                    flow_doc = frappe.get_doc({
                        "doctype": "WhatsApp Flow",
                        "flow_name": flow_name or f"Flow {flow_id}",
                        "whatsapp_account": whatsapp_account,
                        "flow_id": flow_id,
                        "status": flow.get("status", "Draft").title(),
                        "category": flow["categories"][0] if flow.get("categories") else "OTHER"
                    })

                    # Try to fetch and parse flow JSON
                    flow_json = fetch_flow_json_by_id(whatsapp_account, flow_id)
                    if flow_json:
                        flow_doc.flow_json = json.dumps(flow_json, indent=2)
                        flow_doc.data_api_version = flow_json.get("version", "6.0")
                        parse_flow_json_to_screens(flow_doc, flow_json)

                    flow_doc.flags.ignore_validate = True
                    flow_doc.insert(ignore_permissions=True)
                    result["imported"] += 1
                except Exception as e:
                    frappe.log_error(f"Failed to import flow {flow_id}: {str(e)}")
                    result["skipped"] += 1

        return result

    except Exception as e:
        frappe.throw(_("Failed to sync flows: {0}").format(str(e)))


def parse_flow_json_to_screens(flow_doc, flow_json):
    """Parse flow JSON and create screens and fields in the document.

    Args:
        flow_doc: WhatsApp Flow document
        flow_json: Parsed flow JSON dict
    """
    screens = flow_json.get("screens", [])

    for screen_data in screens:
        # Add screen
        flow_doc.append("screens", {
            "screen_id": screen_data.get("id"),
            "screen_title": screen_data.get("title", ""),
            "terminal": 1 if screen_data.get("terminal") else 0,
            "refresh_on_back": 1 if screen_data.get("refresh_on_back") else 0
        })

        # Parse fields from layout
        layout = screen_data.get("layout", {})
        children = layout.get("children", [])

        for child in children:
            field_type = child.get("type")
            if not field_type:
                continue

            # Map WhatsApp field types to our field types
            field_data = {
                "screen": screen_data.get("id"),
                "field_type": field_type,
                "field_name": child.get("name", field_type.lower()),
                "label": child.get("label") or child.get("text", ""),
                "required": 1 if child.get("required") else 0,
                "enabled": 1,
                "helper_text": child.get("helper-text", ""),
                "init_value": child.get("init-value", ""),
                "min_chars": child.get("min-chars"),
                "max_chars": child.get("max-chars"),
                "error_message": child.get("error-message", "")
            }

            # Handle options for dropdowns
            if field_type in ["Dropdown", "RadioButtonsGroup", "CheckboxGroup"]:
                data_source = child.get("data-source", [])
                if data_source:
                    field_data["options"] = json.dumps(data_source)

            flow_doc.append("fields", field_data)
