package com.legacy.uikit.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material.MaterialTheme
import androidx.compose.material.Typography
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

val Inter = FontFamily(Font(R.font.inter_regular), Font(R.font.inter_bold, FontWeight.Bold))

private val Body = TextStyle(fontFamily = Inter, fontWeight = FontWeight.Normal, fontSize = 16.sp, lineHeight = 22.sp)

val LegacyTypography = Typography(
    h4 = TextStyle(fontFamily = Inter, fontWeight = FontWeight.Bold, fontSize = 28.sp, lineHeight = 34.sp),
    h5 = TextStyle(fontWeight = FontWeight.W700, fontSize = 24.sp),
    subtitle1 = TextStyle(fontWeight = FontWeight(700), fontSize = 16.sp),
    body1 = Body,
    body2 = Body.copy(fontSize = 14.sp, lineHeight = 20.sp),
)

object Dimens {
    val spacingXs = 4.dp
    val spacingS = 8.dp
    val spacingM = 16.dp
    val spacingL = 24.dp
    val cornerRadiusS = 8.dp
    val cornerRadiusM = 12.dp
    val iconSize = 24.dp
    val borderThin = 1.dp
}

object AppTheme {
    val colors: AppColors
        @Composable get() = LocalAppColors.current
}

/** The app theme. */
@Composable
fun LegacyAppTheme(dark: Boolean = isSystemInDarkTheme(), content: @Composable () -> Unit) {
    val colors = if (dark) darkAppColors() else lightAppColors()
    CompositionLocalProvider(LocalAppColors provides colors) {
        MaterialTheme(typography = LegacyTypography, content = content)
    }
}
