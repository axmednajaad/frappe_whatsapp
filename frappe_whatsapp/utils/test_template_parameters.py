from types import SimpleNamespace
from unittest import TestCase

import frappe

from frappe_whatsapp.utils.template_parameters import (
	FORMAT_NAME,
	FORMAT_NUMBER,
	build_component_example,
	build_parameters,
	build_text_parameter,
	parse_placeholders,
)


class TestTemplateParameters(TestCase):
	def test_named_and_positional_placeholders(self):
		self.assertEqual(
			parse_placeholders(
				"Hello {{customer_name}}", FORMAT_NAME, "Body"
			),
			(FORMAT_NAME, ["customer_name"]),
		)
		self.assertEqual(
			parse_placeholders("Hello {{1}}, {{2}}", FORMAT_NUMBER, "Body"),
			(FORMAT_NUMBER, [1, 2]),
		)

	def test_rejects_mixed_and_non_contiguous_placeholders(self):
		with self.assertRaises(frappe.ValidationError):
			parse_placeholders("Hello {{1}} {{customer_name}}", None, "Body")
		with self.assertRaises(frappe.ValidationError):
			parse_placeholders("Hello {{1}} {{3}}", FORMAT_NUMBER, "Body")

	def test_named_body_example(self):
		rows = [
			SimpleNamespace(
				position=1,
				parameter_name="customer_name",
				sample_value="Pablo",
			)
		]
		self.assertEqual(
			build_component_example("BODY", rows, FORMAT_NAME),
			{
				"body_text_named_params": [
					{"param_name": "customer_name", "example": "Pablo"}
				]
			},
		)

	def test_url_values_are_percent_encoded(self):
		self.assertEqual(
			build_text_parameter(
				"Gonçalves: New York",
				"customer_name",
				encode_url=True,
			),
			{
				"type": "text",
				"parameter_name": "customer_name",
				"text": "Gon%C3%A7alves%3A%20New%20York",
			},
		)

	def test_named_send_parameters_use_explicit_variable_metadata(self):
		template = frappe._dict(
			parameter_format=FORMAT_NAME,
			variables=[
				frappe._dict(
					component="BODY",
					button_index=0,
					position=1,
					parameter_name="customer_name",
				)
			],
		)
		self.assertEqual(
			build_parameters(template, "BODY", ["Pablo"]),
			[
				{
					"type": "text",
					"parameter_name": "customer_name",
					"text": "Pablo",
				}
			],
		)

	def test_button_allows_number_on_named_template(self):
		self.assertEqual(
			parse_placeholders(
				"https://example.com/?ref_id={{1}}&nocache=1",
				FORMAT_NAME,
				"URL button 1",
				allow_number_when_named=True,
			),
			(FORMAT_NUMBER, [1]),
		)
		template = frappe._dict(
			parameter_format=FORMAT_NAME,
			variables=[
				frappe._dict(
					component="BUTTON",
					button_index=0,
					position=1,
					parameter_name="1",
				)
			],
		)
		self.assertEqual(
			build_parameters(
				template, "BUTTON", ["49624"], button_index=0, encode_url=True
			),
			[{"type": "text", "text": "49624"}],
		)

	def test_normalize_button_sample_extracts_ref_id(self):
		from frappe_whatsapp.utils.template_parameters import normalize_button_sample

		self.assertEqual(
			normalize_button_sample(
				"https://authentications.mfa.gov.so/?ref_id={{1}}&nocache=1",
				"https://authentications.mfa.gov.so/?nocache=1784293266876&ref_id=49624",
			),
			"49624",
		)
