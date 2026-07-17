import re

import frappe


NAMED_PATTERN = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")
POSITIONAL_PATTERN = re.compile(r"\{\{(\d+)\}\}")


def execute():
	frappe.db.sql(
		"""
		UPDATE `tabWhatsApp Templates`
		SET category = CASE
			WHEN category = 'TRANSACTIONAL' THEN 'UTILITY'
			WHEN category = 'OTP' THEN 'AUTHENTICATION'
			ELSE category
		END
		WHERE category IN ('TRANSACTIONAL', 'OTP')
		"""
	)

	for name in frappe.get_all("WhatsApp Templates", pluck="name"):
		doc = frappe.get_doc("WhatsApp Templates", name)
		if doc.variables:
			continue

		format_name = _infer_parameter_format(doc)
		template_type = _infer_template_type(doc)
		frappe.db.set_value(
			"WhatsApp Templates",
			name,
			{
				"parameter_format": format_name,
				"template_type": template_type,
				"meta_managed": int(template_type != "Default"),
			},
			update_modified=False,
		)

		body_samples = _split_legacy(doc.sample_values)
		body_fields = _split_legacy(doc.field_names)
		_insert_variables(
			doc,
			"BODY",
			doc.template,
			body_samples,
			body_fields,
		)

		if doc.header_type == "TEXT":
			_insert_variables(doc, "HEADER", doc.header, [])

		for button_index, button in enumerate(doc.buttons):
			if button.button_type != "Visit Website" or button.url_type != "Dynamic":
				continue
			_insert_variables(
				doc,
				"BUTTON",
				button.website_url,
				_split_legacy(button.example_url),
				button_index=button_index,
			)


def _infer_parameter_format(doc):
	texts = [doc.template or "", doc.header or ""]
	texts.extend(button.website_url or "" for button in doc.buttons)
	return "Name" if any(NAMED_PATTERN.search(text) for text in texts) else "Number"


def _infer_template_type(doc):
	if doc.category == "AUTHENTICATION":
		return "One-Time Passcode"
	if any(button.button_type == "Catalog" for button in doc.buttons):
		return "Catalog"
	return "Default"


def _split_legacy(value):
	if not value:
		return []
	return [part.strip() for part in value.split(",")]


def _insert_variables(
	doc,
	component,
	text,
	samples,
	field_names=None,
	button_index=0,
):
	field_names = field_names or []
	named = NAMED_PATTERN.findall(text or "")
	positional = POSITIONAL_PATTERN.findall(text or "")
	keys = named or positional

	for index, key in enumerate(keys, start=1):
		row = frappe.get_doc(
			{
				"doctype": "WhatsApp Template Variable",
				"parent": doc.name,
				"parenttype": doc.doctype,
				"parentfield": "variables",
				"idx": len(doc.variables) + 1,
				"component": component,
				"button_index": button_index,
				"position": index,
				"parameter_name": key if named else "",
				"sample_value": samples[index - 1] if index <= len(samples) else "",
				"field_name": field_names[index - 1] if index <= len(field_names) else "",
			}
		)
		row.db_insert()
		doc.variables.append(row)
