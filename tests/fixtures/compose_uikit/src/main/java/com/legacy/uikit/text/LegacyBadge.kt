package com.legacy.uikit.text

import androidx.compose.ui.graphics.Color
import com.legacy.uikit.theme.Gray500

data class BadgeUiState(val label: String, val textColor: Color = Gray500)

object BadgeDefaults {
    val Height = 20.dp
    val Background = Color(0xFFABCDEF)
}

val BadgeTextColor = Color(0xFF123456)

@Composable
fun Box(modifier: Modifier = Modifier, content: @Composable () -> Unit) {
    androidx.compose.foundation.layout.Box(modifier = modifier) { content() }
}

@Composable
fun BadgePreviewState() = BadgeUiState(label = "1")
