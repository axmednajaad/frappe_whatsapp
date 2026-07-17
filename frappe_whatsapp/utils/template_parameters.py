"""Helpers for Meta WhatsApp template parameters (positional and named)."""

import re

NAMED_PARAM_PATTERN = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")
POSITIONAL_PARAM_PATTERN = re.compile(r"\{\{(\d+)\}\}")


def extract_named_parameter_names(text):
	"""Return ordered named placeholders from template text, e.g. {{customer_name}}."""
	if not text:
		return []
	return NAMED_PARAM_PATTERN.findall(text)


def uses_named_parameters(template_text, button_urls=None):
	"""True when template uses Meta named parameter format."""
	if extract_named_parameter_names(template_text):
		return True
	for url in button_urls or []:
		if extract_named_parameter_names(url):
			return True
	return False


def resolve_button_parameter_name(button_url, body_template_text):
	"""Resolve Meta parameter name for a dynamic URL button."""
	url_param_names = extract_named_parameter_names(button_url or "")
	if url_param_names:
		return url_param_names[0]

	body_named_params = extract_named_parameter_names(body_template_text or "")
	if body_named_params and POSITIONAL_PARAM_PATTERN.search(button_url or ""):
		# Meta named templates may still sync URL suffix as {{1}}
		return body_named_params[-1]

	return None


def coerce_template_text(value):
	"""Meta requires template parameter values to be strings."""
	if value is None:
		return ""
	return str(value)


def build_text_parameter(value, parameter_name=None):
	"""Build a single WhatsApp template text parameter payload."""
	param = {"type": "text", "text": coerce_template_text(value)}
	if parameter_name:
		param["parameter_name"] = parameter_name
	return param


def build_body_parameters(template_text, field_values):
	"""Build body parameters for positional or named templates.

	field_values: ordered list of values from notification/message field mapping.
	"""
	named_params = extract_named_parameter_names(template_text)
	parameters = []

	for idx, value in enumerate(field_values):
		param_name = named_params[idx] if idx < len(named_params) else None
		parameters.append(build_text_parameter(value, param_name))

	return parameters
