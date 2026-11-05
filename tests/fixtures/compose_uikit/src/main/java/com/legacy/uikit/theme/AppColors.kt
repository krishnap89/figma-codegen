package com.legacy.uikit.theme

import androidx.compose.runtime.*
import androidx.compose.ui.graphics.Color

@Stable
class AppColors(
    textPrimary: Color,
    textSecondary: Color,
    link: Color,
    background: Color,
    accentTint: Color,
) {
    var textPrimary by mutableStateOf(textPrimary)
        private set
    var textSecondary by mutableStateOf(textSecondary)
        private set
    var link by mutableStateOf(link)
        private set
    var background by mutableStateOf(background)
        private set
    var accentTint by mutableStateOf(accentTint)
        private set
}

fun lightAppColors() = AppColors(
    textPrimary = Gray900,
    textSecondary = Gray500,
    link = Blue600,
    background = White,
    accentTint = Color(0xFF00AAB2).copy(alpha = 0.05f),
)

fun darkAppColors() = AppColors(
    textPrimary = White,
    textSecondary = Color(0xFF9CA3AF),
    link = Color(0xFF60A5FA),
    background = Gray900,
    accentTint = Color(0x1A00AAB2),
)

internal val LocalAppColors = staticCompositionLocalOf { lightAppColors() }
