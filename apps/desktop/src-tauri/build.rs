fn main() {
    tauri_build::try_build(tauri_build::Attributes::new().plugin(
        "desktop-approval",
        tauri_build::InlinedPlugin::new().commands(&["desktop_shell_answer"]),
    ))
    .expect("build desktop permissions")
}
