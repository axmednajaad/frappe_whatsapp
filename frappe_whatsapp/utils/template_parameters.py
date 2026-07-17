"""Meta WhatsApp template-variable parsing and payload helpers."""

import re
from urllib.parse import quote

import frappe
from frappe import _

NAMED_PARAM_PATTERN = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")
POSITIONAL_PARAM_PATTERN = re.compile(r"\{\{(\d+)\}\}")

FORMAT_NAME = "Name"
FORMAT_NUMBER = "Number"
API_FORMATS = {FORMAT_NAME: "named", FORMAT_NUMBER: "positional"}


def extract_named_parameter_names(text):
	"""Return ordered named placeholders from text."""
	return NAMED_PARAM_PATTERN.findall(text or "")


def extract_positional_parameter_numbers(text):
	"""Return ordered positional placeholders from text."""
	return [int(value) for value in POSITIONAL_PARAM_PATTERN.findall(text or "")]


def parse_placeholders(
	text, expected_format=None, label=None, allow_number_when_named=False
):
	"""Parse and validate one component's placeholders.

	Meta Visit Website buttons commonly use Number placeholders ({{1}})
	even when the template Type of Variable is Name.
	"""
	named = extract_named_parameter_names(text)
	positional = extract_positional_parameter_numbers(text)
	label = label or _("Template component")

	if named and positional:
		frappe.throw(_("{0} cannot mix Name and Number variables.").format(label))

	actual_format = FORMAT_NAME if named else FORMAT_NUMBER if positional else None
	if actual_format and expected_format and actual_format != expected_format:
		# Named templates may still use {{1}} on dynamic URL buttons.
		if not (
			allow_number_when_named
			and expected_format == FORMAT_NAME
			and actual_format == FORMAT_NUMBER
		):
			frappe.throw(
				_("{0} uses {1} variables but Type of Variable is {2}.").format(
					label, actual_format, expected_format
				)
			)

	if named and len(named) != len(set(named)):
		frappe.throw(_("{0} contains duplicate named variables.").format(label))

	if positional:
		expected = list(range(1, len(positional) + 1))
		if positional != expected:
			frappe.throw(
				_("{0} Number variables must be contiguous and ordered from {{1}}.").format(
					label
				)
			)

	return actual_format, named or positional


def uses_named_parameters(template_text, button_urls=None):
	texts = [template_text or "", *(button_urls or [])]
	return any(extract_named_parameter_names(text) for text in texts)


def to_api_parameter_format(value):
	try:
		return API_FORMATS[value]
	except KeyError:
		frappe.throw(_("Type of Variable must be Name or Number."))


def from_api_parameter_format(value, component_texts=None):
	value = (value or "").upper()
	if value == "NAMED":
		return FORMAT_NAME
	if value == "POSITIONAL":
		return FORMAT_NUMBER
	return (
		FORMAT_NAME
		if any(extract_named_parameter_names(text) for text in (component_texts or []))
		else FORMAT_NUMBER
	)


def api_parameter_name(name):
	"""Return Meta named parameter_name, or None for Number placeholders."""
	name = (name or "").strip()
	if not name:
		return None
	# Number placeholders are stored as "1", "2" for UI display only.
	if name.isdigit():
		return None
	if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
		return None
	return name


def normalize_button_sample(button_url, example):
	"""Meta often returns a full example URL; keep only the dynamic value."""
	example = coerce_template_text(example).strip()
	if not example:
		return ""
	if not example.startswith(("http://", "https://")):
		return example

	from urllib.parse import parse_qs, urlparse

	query = parse_qs(urlparse(example).query)
	for key in ("ref_id", "tracking_number", "id", "document_id"):
		if query.get(key) and query[key][0]:
			return query[key][0]

	path = urlparse(example).path.rstrip("/")
	if path and path != "/":
		return path.split("/")[-1]

	# Fallback: if template URL has {{1}} at the end, take trailing segment after static prefix.
	static_prefix = re.split(r"\{\{\d+\}\}", button_url or "", maxsplit=1)[0]
	if static_prefix and example.startswith(static_prefix):
		return example[len(static_prefix) :]

	return example


def coerce_template_text(value):
	return "" if value is None else str(value)


def build_text_parameter(value, parameter_name=None, encode_url=False):
	text = coerce_template_text(value)
	if encode_url:
		text = quote(text, safe="")
	param = {"type": "text", "text": text}
	parameter_name = api_parameter_name(parameter_name)
	if parameter_name:
		param["parameter_name"] = parameter_name
	return param


def get_component_variables(template, component, button_index=None):
	rows = [
		row
		for row in (template.get("variables") or [])
		if row.component == component
		and (component != "BUTTON" or row.button_index == button_index)
	]
	return sorted(rows, key=lambda row: row.position)


def build_parameters(template, component, values, button_index=None, encode_url=False):
	rows = get_component_variables(template, component, button_index)
	values = list(values or [])
	if len(rows) != len(values):
		frappe.throw(
			_("{0} expects {1} variable value(s), but received {2}.").format(
				component.title(), len(rows), len(values)
			)
		)

	return [
		build_text_parameter(
			value,
			row.parameter_name,
			encode_url=encode_url,
		)
		for row, value in zip(rows, values)
	]


def build_body_parameters(template_text, field_values, parameter_format=None, variables=None):
	"""Compatibility helper for callers that do not yet pass a template document."""
	expected_format = parameter_format or (
		FORMAT_NAME if extract_named_parameter_names(template_text) else FORMAT_NUMBER
	)
	_, keys = parse_placeholders(template_text, expected_format, _("Template body"))
	values = list(field_values or [])
	if len(keys) != len(values):
		frappe.throw(
			_("Template body expects {0} variable value(s), but received {1}.").format(
				len(keys), len(values)
			)
		)

	names = [row.parameter_name for row in variables] if variables else keys
	return [
		build_text_parameter(
			value,
			names[index] if expected_format == FORMAT_NAME else None,
		)
		for index, value in enumerate(values)
	]


def build_component_example(component, variables, parameter_format):
	rows = sorted(variables, key=lambda row: row.position)
	if not rows:
		return None
	if any(not row.sample_value for row in rows):
		frappe.throw(
			_("Every {0} variable requires a sample value.").format(component.lower())
		)

	if component == "BODY":
		if parameter_format == FORMAT_NAME:
			return {
				"body_text_named_params": [
					{"param_name": row.parameter_name, "example": row.sample_value}
					for row in rows
				]
			}
		return {"body_text": [[row.sample_value for row in rows]]}

	if component == "HEADER":
		if parameter_format == FORMAT_NAME:
			return {
				"header_text_named_params": [
					{"param_name": rows[0].parameter_name, "example": rows[0].sample_value}
				]
			}
		return {"header_text": [rows[0].sample_value]}

	return None


def reconcile_variable_rows(template):
	"""Rebuild variable rows from component text while preserving entered metadata."""
	existing = {
		(row.component, row.button_index or 0, row.parameter_name or str(row.position)): row
		for row in (template.get("variables") or [])
	}
	definitions = [
		("BODY", 0, template.template or "", _("Template body")),
	]
	if template.header_type == "TEXT":
		definitions.append(("HEADER", 0, template.header or "", _("Template header")))
	for button_index, button in enumerate(template.get("buttons") or []):
		if button.button_type == "Visit Website" and button.url_type == "Dynamic":
			definitions.append(
				(
					"BUTTON",
					button_index,
					button.website_url or "",
					_("URL button {0}").format(button_index + 1),
				)
			)

	rows = []
	for component, button_index, text, label in definitions:
		actual_format, keys = parse_placeholders(
			text,
			template.parameter_format,
			label,
			allow_number_when_named=component == "BUTTON",
		)
		use_named = actual_format == FORMAT_NAME
		for position, key in enumerate(keys, start=1):
			# Always store a display value: named key or Number index ("1").
			display_name = str(key)
			identity = (
				component,
				button_index,
				display_name if use_named else str(position),
			)
			old = existing.get(identity) or existing.get(
				(component, button_index, str(position))
			)
			sample_value = old.sample_value if old else ""
			if component == "BUTTON" and not sample_value:
				button = (template.get("buttons") or [])[button_index]
				example = (getattr(button, "example_url", None) or "").split(",")[0]
				sample_value = normalize_button_sample(text, example)
			elif component == "BUTTON" and sample_value:
				sample_value = normalize_button_sample(text, sample_value)

			rows.append(
				{
					"component": component,
					"button_index": button_index,
					"position": position,
					"parameter_name": display_name,
					"sample_value": sample_value,
					"field_name": old.field_name if old else "",
				}
			)

	template.set("variables", rows)


def resolve_button_parameter_name(button_url, body_template_text=None):
	"""Compatibility helper; URL variable names must come from the URL itself."""
	names = extract_named_parameter_names(button_url)
	return names[0] if names else None
