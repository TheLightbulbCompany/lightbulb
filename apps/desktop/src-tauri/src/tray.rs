use tauri::{
    menu::{MenuBuilder, MenuItemBuilder},
    tray::TrayIconBuilder,
    AppHandle,
};

pub fn create_tray(app: &AppHandle) -> tauri::Result<()> {
    // "Open Lightbulb" is the only way back to the window that isn't the Dock: the window can be
    // hidden (close button) or, after a crash, gone; focus_or_recreate_main_window covers both.
    let open = MenuItemBuilder::with_id("open", "Open Lightbulb").build(app)?;
    let quit = MenuItemBuilder::with_id("quit", "Quit").build(app)?;
    let menu = MenuBuilder::new(app).item(&open).item(&quit).build()?;
    TrayIconBuilder::with_id("main")
        .tooltip("Lightbulb")
        .menu(&menu)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "open" => crate::focus_or_recreate_main_window(app),
            "quit" => app.exit(0),
            _ => {}
        })
        .build(app)?;
    Ok(())
}
