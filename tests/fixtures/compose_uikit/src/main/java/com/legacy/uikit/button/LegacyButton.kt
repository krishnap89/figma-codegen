package com.legacy.uikit.button

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier

enum class LegacyButtonStyle { Primary, Secondary, Text }

enum class LegacyButtonSize(val heightDp: Int) { S(32), M(40), L(48); }

/**
 * Primary action button. Use one Primary per screen.
 *
 * @param text label
 */
@Composable
fun LegacyButton(
    text: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    style: LegacyButtonStyle = LegacyButtonStyle.Primary,
    size: LegacyButtonSize = LegacyButtonSize.M,
    enabled: Boolean = true,
) {
}

@Composable
fun LegacyButton(onClick: () -> Unit, content: @Composable () -> Unit) {
}

@Composable
private fun ButtonPreviewHelper() {}

@Preview(showBackground = true)
@Composable
fun LegacyButtonPreview() {
    LegacyButton(text = "Preview", onClick = {})
}
