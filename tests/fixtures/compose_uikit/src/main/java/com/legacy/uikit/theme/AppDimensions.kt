package com.legacy.uikit.theme

import androidx.compose.material.MaterialTheme
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp

data class AppDimensions(
    /** 12dp */
    val spacingMedium: Dp = 12.dp,
    val radiusLarge: Dp = 40.dp,
)

internal val LocalAppDimensions = staticCompositionLocalOf { AppDimensions() }

val MaterialTheme.dimens: AppDimensions
    @Composable
    @ReadOnlyComposable
    get() = LocalAppDimensions.current
