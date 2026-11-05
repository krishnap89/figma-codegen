package com.legacy.uikit.input

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier

/** Single-line input, with label and optional error. */
@Composable
fun LegacyTextField(
    value: String,
    onValueChange: (String) -> Unit,
    label: String,
    modifier: Modifier = Modifier,
    placeholder: String? = null,
    error: String? = null,
) {
}

@Composable
fun LegacyCheckBox(checked: Boolean, onCheckedChange: (Boolean) -> Unit, enabled: Boolean = true) {}

@Deprecated("Use LegacyTextField")
@Composable
fun LegacyOldField(value: TextFieldValue, onValueChange: (TextFieldValue) -> Unit) {}
