package com.legacy.app.feature

@Composable
fun LoginScreen(state: LoginState, onLogin: () -> Unit, onEmailChange: (String) -> Unit) {
    Column {
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
