package com.legacy.app.feature

import androidx.compose.foundation.layout.Box
import com.legacy.uikit.button.LegacyButton
import com.legacy.uikit.button.LegacyButtonStyle
import com.legacy.uikit.input.*

@Composable
fun LoginScreen(state: LoginState, onLogin: () -> Unit, onEmailChange: (String) -> Unit) {
    Column {
        Box(Modifier) {}
        LegacyTextField(
            value = state.email,
            onValueChange = onEmailChange,
            label = "Email",
        )
        LegacyButton(text = "Log in", onClick = onLogin, style = LegacyButtonStyle.Primary)
        LegacyButton(
            text = "Create account",
            onClick = {
                // a long lambda body
                analytics.track("create_account_clicked_from_login_screen_with_a_really_long_event_name")
                onCreateAccount()
            },
            style = LegacyButtonStyle.Secondary,
        )
    }
}
