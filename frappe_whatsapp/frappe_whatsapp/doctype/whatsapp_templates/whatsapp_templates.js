// Copyright (c) 2022, Shridhar Patil and contributors
// For license information, please see license.txt

const TEMPLATE_TYPES = {
	MARKETING: ["Default", "Catalog", "Call Permission Request"],
	UTILITY: ["Default", "Call Permission Request"],
	AUTHENTICATION: ["One-Time Passcode"],
};

frappe.ui.form.on("WhatsApp Templates", {
	refresh(frm) {
		set_template_type_options(frm);
		set_meta_managed_state(frm);
	},

	category(frm) {
		set_template_type_options(frm, true);
	},

	parameter_format(frm) {
		sync_variable_rows(frm);
	},

	template(frm) {
		sync_variable_rows(frm);
	},

	header(frm) {
		sync_variable_rows(frm);
	},

	header_type(frm) {
		sync_variable_rows(frm);
	},
});

frappe.ui.form.on("WhatsApp Button", {
	button_type(_frm, _cdt, _cdn) {
		sync_variable_rows(cur_frm);
	},

	url_type(_frm, _cdt, _cdn) {
		sync_variable_rows(cur_frm);
	},

	website_url(_frm, _cdt, _cdn) {
		sync_variable_rows(cur_frm);
	},

	buttons_remove(frm) {
		sync_variable_rows(frm);
	},
});

function set_template_type_options(frm, reset_invalid = false) {
	const options = TEMPLATE_TYPES[frm.doc.category] || [];
	frm.set_df_property("template_type", "options", options.join("\n"));
	if (
		reset_invalid &&
		options.length &&
		!options.includes(frm.doc.template_type)
	) {
		frm.set_value("template_type", options[0]);
	}
}

function set_meta_managed_state(frm) {
	if (!frm.doc.meta_managed) {
		return;
	}
	[
		"category",
		"template_type",
		"parameter_format",
		"template",
		"header_type",
		"header",
		"footer",
		"buttons",
		"variables",
	].forEach((fieldname) => frm.set_df_property(fieldname, "read_only", 1));
	frm.dashboard.set_headline(
		__(
			"This specialized template is managed in Meta. Synchronize again after changing it there."
		)
	);
}

function sync_variable_rows(frm) {
	if (frm.doc.meta_managed) {
		return;
	}

	const definitions = [
		{ component: "BODY", button_index: 0, text: frm.doc.template || "" },
	];
	if (frm.doc.header_type === "TEXT") {
		definitions.push({
			component: "HEADER",
			button_index: 0,
			text: frm.doc.header || "",
		});
	}
	(frm.doc.buttons || []).forEach((button, button_index) => {
		if (
			button.button_type === "Visit Website" &&
			button.url_type === "Dynamic"
		) {
			definitions.push({
				component: "BUTTON",
				button_index,
				text: button.website_url || "",
			});
		}
	});

	const existing = new Map(
		(frm.doc.variables || []).map((row) => [
			variable_key(
				row.component,
				row.button_index || 0,
				row.parameter_name || String(row.position)
			),
			row,
		])
	);
	const rows = [];
	definitions.forEach((definition) => {
		const parsed = parse_variables(
			definition.text,
			frm.doc.parameter_format,
			definition.component === "BUTTON"
		);
		parsed.keys.forEach((key, index) => {
			const position = index + 1;
			const useNamed = parsed.format === "Name";
			const displayName = String(key);
			const identity = variable_key(
				definition.component,
				definition.button_index,
				useNamed ? displayName : String(position)
			);
			const old =
				existing.get(identity) ||
				existing.get(
					variable_key(
						definition.component,
						definition.button_index,
						String(position)
					)
				);
			let sampleValue = old?.sample_value || "";
			if (definition.component === "BUTTON") {
				sampleValue = normalizeButtonSample(
					definition.text,
					sampleValue ||
						(frm.doc.buttons?.[definition.button_index]?.example_url || "")
							.split(",")[0]
				);
			}
			rows.push({
				component: definition.component,
				button_index: definition.button_index,
				position,
				parameter_name: displayName,
				sample_value: sampleValue,
				field_name: old?.field_name || "",
			});
		});
	});

	frm.clear_table("variables");
	rows.forEach((values) => Object.assign(frm.add_child("variables"), values));
	frm.refresh_field("variables");
}

function normalizeButtonSample(buttonUrl, example) {
	example = (example || "").trim();
	if (!example) {
		return "";
	}
	if (!/^https?:\/\//i.test(example)) {
		return example;
	}
	try {
		const url = new URL(example);
		for (const key of ["ref_id", "tracking_number", "id", "document_id"]) {
			const value = url.searchParams.get(key);
			if (value) {
				return value;
			}
		}
		const parts = url.pathname.replace(/\/+$/, "").split("/");
		const last = parts[parts.length - 1];
		if (last) {
			return last;
		}
	} catch (e) {
		// keep original example
	}
	return example;
}

function parse_variables(text, parameter_format, allowNumberWhenNamed = false) {
	const namedPattern = /\{\{([a-z][a-z0-9_]*)\}\}/g;
	const numberPattern = /\{\{(\d+)\}\}/g;
	const named = [...text.matchAll(namedPattern)].map((match) => match[1]);
	const numbered = [...text.matchAll(numberPattern)].map((match) => match[1]);

	if (named.length && numbered.length) {
		frappe.msgprint(__("A component cannot mix Name and Number variables."));
		return { format: null, keys: [] };
	}

	if (named.length) {
		if (parameter_format === "Number") {
			frappe.msgprint(
				__("This component uses Name variables but Type of Variable is Number.")
			);
			return { format: null, keys: [] };
		}
		return { format: "Name", keys: named };
	}

	if (numbered.length) {
		if (
			parameter_format === "Name" &&
			!allowNumberWhenNamed
		) {
			frappe.msgprint(
				__("This component uses Number variables but Type of Variable is Name.")
			);
			return { format: null, keys: [] };
		}
		return { format: "Number", keys: numbered };
	}

	return { format: null, keys: [] };
}

function variable_key(component, button_index, key) {
	return `${component}:${button_index}:${key}`;
}
