# Copyright (c) 2022, Shridhar Patil and Contributors
# See license.txt

import json
from unittest.mock import patch, MagicMock

import frappe
from frappe_whatsapp.testing import IntegrationTestCase


class TestWhatsAppTemplates(IntegrationTestCase):
    """Tests for WhatsApp Templates doctype."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ensure_test_account()

    @classmethod
    def _ensure_test_account(cls):
        if not frappe.db.exists("WhatsApp Account", "Test WA Tmpl Account"):
            account = frappe.get_doc({
                "doctype": "WhatsApp Account",
                "account_name": "Test WA Tmpl Account",
                "status": "Active",
                "url": "https://graph.facebook.com",
                "version": "v17.0",
                "phone_id": "tmpl_test_phone_id",
                "business_id": "tmpl_test_business_id",
                "app_id": "tmpl_test_app_id",
                "webhook_verify_token": "tmpl_test_verify_token",
                "is_default_incoming": 1,
                "is_default_outgoing": 1,
            })
            account.insert(ignore_permissions=True)
            frappe.db.commit()  # nosemgrep: frappe-manual-commit -- test fixture must be visible to later queries

    def setUp(self):
        # Set password within each test's transaction scope
        from frappe.utils.password import set_encrypted_password
        set_encrypted_password("WhatsApp Account", "Test WA Tmpl Account", "test_tmpl_token", "token")
        # Clear ALL defaults then set ours (db.set_value bypasses on_update hooks)
        frappe.db.sql("UPDATE `tabWhatsApp Account` SET is_default_outgoing=0, is_default_incoming=0")
        frappe.db.set_value("WhatsApp Account", "Test WA Tmpl Account", {
            "is_default_outgoing": 1,
            "is_default_incoming": 1,
        })

    def tearDown(self):
        # Use SQL-level delete to avoid triggering on_trash (which calls get_settings)
        frappe.db.delete("WhatsApp Templates", {"template_name": ["like", "test_tmpl_%"]})
        frappe.db.delete("WhatsApp Templates", {"template_name": ["like", "test_msg_template%"]})
        frappe.db.commit()  # nosemgrep: frappe-manual-commit -- test fixture must be visible to later queries

    def _make_template_without_hooks(self, **kwargs):
        """Create a template directly in DB to avoid Meta API calls."""
        template_name = kwargs.get("template_name", "test_tmpl_basic")
        language_code = kwargs.get("language_code", "en")
        doc = frappe.get_doc({
            "doctype": "WhatsApp Templates",
            "template_name": template_name,
            "actual_name": template_name.lower().replace(" ", "_"),
            "template": kwargs.get("template", "Hello {{1}}"),
            "category": kwargs.get("category", "UTILITY"),
            "language": kwargs.get("language", frappe.db.get_value("Language", {"language_code": "en"}) or "en"),
            "language_code": language_code,
            "whatsapp_account": kwargs.get("whatsapp_account", "Test WA Tmpl Account"),
            "status": kwargs.get("status", "APPROVED"),
            "id": kwargs.get("id", f"tmpl_id_{template_name}"),
            "header_type": kwargs.get("header_type", ""),
            "header": kwargs.get("header", ""),
            "footer": kwargs.get("footer", ""),
            "sample_values": kwargs.get("sample_values", ""),
            "parameter_format": kwargs.get("parameter_format", "Number"),
            "template_type": kwargs.get("template_type", "Default"),
        })
        doc.db_insert()
        frappe.db.commit()  # nosemgrep: frappe-manual-commit -- test fixture must be visible to later queries
        return frappe.get_doc("WhatsApp Templates", doc.name)

    def test_template_autoname(self):
        """Test template autoname format: template_name-language_code."""
        doc = self._make_template_without_hooks(template_name="test_tmpl_autoname")
        self.assertEqual(doc.name, "test_tmpl_autoname-en")

    @patch("frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates.make_post_request")
    def test_language_code_set_on_validate(self, mock_post):
        """Test language_code is derived from language field on validate."""
        mock_post.return_value = {}
        doc = self._make_template_without_hooks(template_name="test_tmpl_langcode")
        doc.language_code = ""
        doc.sample_values = "World"
        doc.language = frappe.db.get_value("Language", {"language_code": "en"}) or "en"
        doc.validate()
        self.assertTrue(len(doc.language_code) > 0)

    def test_set_whatsapp_account_default(self):
        """Test whatsapp_account is set to default if missing."""
        doc = self._make_template_without_hooks(
            template_name="test_tmpl_default_acct",
            whatsapp_account=""
        )
        doc.whatsapp_account = ""
        doc.set_whatsapp_account()
        self.assertTrue(len(doc.whatsapp_account) > 0)

    def test_read_local_file_uses_file_doctype(self):
        """_read_local_file resolves through the File doctype, not raw paths."""
        doc = self._make_template_without_hooks(template_name="test_tmpl_path")

        mock_file = MagicMock()
        mock_file.get_content.return_value = b"binary-content"
        with patch("frappe.get_doc", return_value=mock_file) as mock_get_doc:
            content = doc._read_local_file("/files/test_image.png")

        self.assertEqual(content, b"binary-content")
        mock_get_doc.assert_called_once_with("File", {"file_url": "/files/test_image.png"})

    def test_get_header_text(self):
        """Test get_header for TEXT header type."""
        doc = self._make_template_without_hooks(
            template_name="test_tmpl_hdr_text",
            header_type="TEXT",
            header="Order Update"
        )
        header = doc.get_header()
        self.assertEqual(header["type"], "HEADER")
        self.assertEqual(header["format"], "TEXT")
        self.assertEqual(header["text"], "Order Update")

    def test_get_header_text_with_sample(self):
        """Test get_header for TEXT header with sample values."""
        doc = self._make_template_without_hooks(
            template_name="test_tmpl_hdr_sample",
            header_type="TEXT",
            header="Hello {{1}}",
            sample_values="John"
        )
        doc.sample = "John"
        header = doc.get_header()
        self.assertEqual(header["format"], "TEXT")
        self.assertIn("example", header)
        self.assertEqual(header["example"]["header_text"], ["John"])

    def test_get_settings(self):
        """Test get_settings loads WhatsApp Account credentials."""
        doc = self._make_template_without_hooks(template_name="test_tmpl_settings")
        doc.get_settings()
        self.assertEqual(doc._url, "https://graph.facebook.com")
        self.assertEqual(doc._version, "v17.0")
        self.assertEqual(doc._business_id, "tmpl_test_business_id")

    @patch("frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates.make_post_request")
    def test_after_insert_creates_template_on_meta(self, mock_post):
        """Test after_insert sends template to Meta API."""
        mock_post.return_value = {
            "id": "new_template_id_123",
            "status": "PENDING",
        }

        doc = frappe.get_doc({
            "doctype": "WhatsApp Templates",
            "template_name": "test_tmpl_insert",
            "template": "Test body {{1}}",
            "sample_values": "World",
            "category": "UTILITY",
            "language": frappe.db.get_value("Language", {"language_code": "en"}) or "en",
            "language_code": "en",
            "whatsapp_account": "Test WA Tmpl Account",
        })
        doc.insert(ignore_permissions=True)

        self.assertTrue(mock_post.called)
        call_args = mock_post.call_args
        sent_data = json.loads(call_args.kwargs.get("data", call_args[1].get("data", "")))
        self.assertEqual(sent_data["name"], "test_tmpl_insert")
        self.assertEqual(sent_data["language"], "en")
        self.assertEqual(sent_data["category"], "UTILITY")
        self.assertEqual(sent_data["parameter_format"], "positional")
        self.assertTrue(any(c["type"] == "BODY" for c in sent_data["components"]))

    @patch("frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates.make_post_request")
    def test_after_insert_creates_named_template_with_samples(self, mock_post):
        mock_post.return_value = {
            "id": "new_named_template_id",
            "status": "PENDING",
        }
        doc = frappe.get_doc({
            "doctype": "WhatsApp Templates",
            "template_name": "test_tmpl_named",
            "template": "Hello {{customer_name}}, reference {{reference_id}}",
            "parameter_format": "Name",
            "template_type": "Default",
            "category": "UTILITY",
            "language": frappe.db.get_value(
                "Language", {"language_code": "en"}
            ) or "en",
            "language_code": "en",
            "whatsapp_account": "Test WA Tmpl Account",
            "variables": [
                {
                    "component": "BODY",
                    "position": 1,
                    "parameter_name": "customer_name",
                    "sample_value": "Pablo",
                },
                {
                    "component": "BODY",
                    "position": 2,
                    "parameter_name": "reference_id",
                    "sample_value": "REF-001",
                },
            ],
        })
        doc.insert(ignore_permissions=True)

        sent_data = json.loads(mock_post.call_args.kwargs["data"])
        self.assertEqual(sent_data["parameter_format"], "named")
        body = next(
            component
            for component in sent_data["components"]
            if component["type"] == "BODY"
        )
        self.assertEqual(
            body["example"]["body_text_named_params"],
            [
                {"param_name": "customer_name", "example": "Pablo"},
                {"param_name": "reference_id", "example": "REF-001"},
            ],
        )

    @patch("frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates.make_post_request")
    def test_after_insert_with_footer(self, mock_post):
        """Test template creation includes footer in components."""
        mock_post.return_value = {"id": "tmpl_footer_id", "status": "PENDING"}

        doc = frappe.get_doc({
            "doctype": "WhatsApp Templates",
            "template_name": "test_tmpl_footer",
            "template": "Body text",
            "footer": "Reply STOP to opt out",
            "category": "MARKETING",
            "language": frappe.db.get_value("Language", {"language_code": "en"}) or "en",
            "language_code": "en",
            "whatsapp_account": "Test WA Tmpl Account",
        })
        doc.insert(ignore_permissions=True)

        call_args = mock_post.call_args
        sent_data = json.loads(call_args.kwargs.get("data", call_args[1].get("data", "")))
        footer_components = [c for c in sent_data["components"] if c["type"] == "FOOTER"]
        self.assertEqual(len(footer_components), 1)
        self.assertEqual(footer_components[0]["text"], "Reply STOP to opt out")

    @patch("frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates.make_post_request")
    def test_after_insert_with_buttons(self, mock_post):
        """Test template creation includes buttons."""
        mock_post.return_value = {"id": "tmpl_btn_id", "status": "PENDING"}

        doc = frappe.get_doc({
            "doctype": "WhatsApp Templates",
            "template_name": "test_tmpl_buttons",
            "template": "Click below",
            "category": "UTILITY",
            "language": frappe.db.get_value("Language", {"language_code": "en"}) or "en",
            "language_code": "en",
            "whatsapp_account": "Test WA Tmpl Account",
        })
        doc.append("buttons", {
            "button_type": "Quick Reply",
            "button_label": "Yes",
        })
        doc.append("buttons", {
            "button_type": "Visit Website",
            "button_label": "Visit",
            "website_url": "https://example.com",
            "url_type": "Static",
        })
        doc.insert(ignore_permissions=True)

        call_args = mock_post.call_args
        sent_data = json.loads(call_args.kwargs.get("data", call_args[1].get("data", "")))
        button_components = [c for c in sent_data["components"] if c["type"] == "BUTTONS"]
        self.assertEqual(len(button_components), 1)
        buttons = button_components[0]["buttons"]
        self.assertEqual(len(buttons), 2)
        self.assertEqual(buttons[0]["type"], "QUICK_REPLY")
        self.assertEqual(buttons[1]["type"], "URL")

    @patch("frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates.make_post_request")
    @patch("frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates.make_request")
    def test_on_trash_deletes_from_meta(self, mock_request, mock_post):
        """Test on_trash calls Meta API to delete template."""
        mock_post.return_value = {"id": "tmpl_trash_id", "status": "PENDING"}

        doc = frappe.get_doc({
            "doctype": "WhatsApp Templates",
            "template_name": "test_tmpl_trash",
            "template": "Delete me",
            "category": "UTILITY",
            "language": frappe.db.get_value("Language", {"language_code": "en"}) or "en",
            "language_code": "en",
            "whatsapp_account": "Test WA Tmpl Account",
        })
        doc.insert(ignore_permissions=True)

        mock_request.return_value = {}
        doc.delete()

        # Verify DELETE was called on Meta API
        self.assertTrue(mock_request.called)
        delete_call = mock_request.call_args
        self.assertEqual(delete_call[0][0], "DELETE")
        self.assertIn("message_templates", delete_call[0][1])

    @patch("frappe.model.document.Document.get_password", return_value="mock_token")
    @patch("frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates.make_request")
    @patch("frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates.make_post_request")
    def test_fetch_templates_from_meta(self, mock_post, mock_get, mock_get_password):
        """Test the fetch whitelisted function."""
        mock_get.return_value = {
            "data": [
                {
                    "name": "test_tmpl_fetched",
                    "status": "APPROVED",
                    "language": "en",
                    "category": "UTILITY",
                    "parameter_format": "POSITIONAL",
                    "id": "fetched_tmpl_id",
                    "components": [
                        {
                            "type": "BODY",
                            "text": "Hello {{1}}, your order is ready",
                            "example": {"body_text": [["Pablo"]]},
                        },
                        {"type": "FOOTER", "text": "Thank you"},
                    ]
                }
            ]
        }

        from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates import fetch
        result = fetch()
        self.assertIn("Successfully fetched", result)
        fetch()
        self.assertEqual(
            frappe.db.count(
                "WhatsApp Templates", {"id": "fetched_tmpl_id"}
            ),
            1,
        )
        template = frappe.get_doc(
            "WhatsApp Templates", {"id": "fetched_tmpl_id"}
        )
        self.assertEqual(template.parameter_format, "Number")
        self.assertEqual(template.variables[0].sample_value, "Pablo")

    @patch("frappe.model.document.Document.get_password", return_value="mock_token")
    @patch("frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates.make_request")
    def test_fetch_named_samples_and_specialized_type(
        self, mock_get, _mock_get_password
    ):
        mock_get.return_value = {
            "data": [
                {
                    "name": "test_tmpl_named_fetched",
                    "status": "APPROVED",
                    "language": "en",
                    "category": "UTILITY",
                    "parameter_format": "NAMED",
                    "id": "fetched_named_tmpl_id",
                    "components": [
                        {
                            "type": "BODY",
                            "text": "Hello {{customer_name}}",
                            "example": {
                                "body_text_named_params": [
                                    {
                                        "param_name": "customer_name",
                                        "example": "Pablo",
                                    }
                                ]
                            },
                        },
                        {"type": "CALL_PERMISSION_REQUEST"},
                    ],
                }
            ]
        }

        from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates import fetch

        fetch()
        template = frappe.get_doc(
            "WhatsApp Templates", {"id": "fetched_named_tmpl_id"}
        )
        self.assertEqual(template.parameter_format, "Name")
        self.assertEqual(template.template_type, "Call Permission Request")
        self.assertEqual(template.meta_managed, 1)
        self.assertEqual(template.variables[0].parameter_name, "customer_name")
        self.assertEqual(template.variables[0].sample_value, "Pablo")

    @patch("frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates.make_request")
    def test_fetch_iterator_consumes_pagination(self, mock_get):
        mock_get.side_effect = [
            {
                "data": [{"id": "page-one"}],
                "paging": {
                    "next": "https://graph.facebook.com/next-page",
                    "cursors": {"after": "page-cursor"},
                },
            },
            {"data": [{"id": "page-two"}]},
        ]
        from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates import _iter_meta_templates

        account = frappe._dict(
            url="https://graph.facebook.com",
            version="v25.0",
            business_id="business-id",
        )
        templates = list(_iter_meta_templates(account, {"authorization": "Bearer token"}))
        self.assertEqual(
            [template["id"] for template in templates],
            ["page-one", "page-two"],
        )
        self.assertIn(
            "after=page-cursor",
            mock_get.call_args_list[1].args[1],
        )

    def test_upsert_doc_without_hooks(self):
        """Test upsert_doc_without_hooks inserts and updates correctly."""
        from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates import upsert_doc_without_hooks

        doc = self._make_template_without_hooks(template_name="test_tmpl_upsert")

        # Update template text
        doc.template = "Updated body text"
        upsert_doc_without_hooks(doc, "WhatsApp Button", "buttons")

        doc.reload()
        self.assertEqual(doc.template, "Updated body text")

    def test_migrate_legacy_category_and_variable_metadata(self):
        doc = self._make_template_without_hooks(
            template_name="test_tmpl_migration",
            category="TRANSACTIONAL",
            sample_values="Pablo,REF-001",
        )
        doc.template = "Hello {{1}}, reference {{2}}"
        doc.field_names = "first_name,name"
        doc.db_update()

        from frappe_whatsapp.patches.migrate_template_metadata import execute

        execute()
        doc.reload()
        self.assertEqual(doc.category, "UTILITY")
        self.assertEqual(doc.parameter_format, "Number")
        self.assertEqual(
            [row.sample_value for row in doc.variables],
            ["Pablo", "REF-001"],
        )
        self.assertEqual(
            [row.field_name for row in doc.variables],
            ["first_name", "name"],
        )
