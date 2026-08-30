use tauri::{
    menu::{MenuBuilder, MenuItemBuilder},
    tray::TrayIconBuilder,
    AppHandle, Manager,
};

pub fn create_tray(app: &AppHandle) -> tauri::Result<()> {
    build_tray_menu(app, "Ready")?;
    Ok(())
}

pub fn update_tray_status(app: &AppHandle, label: &str) {
    // Rebuild the tray menu with the new status label
    let _ = build_tray_menu(app, label);
}

fn build_tray_menu(app: &AppHandle, status_label: &str) -> tauri::Result<()> {
    let title = MenuItemBuilder::with_id("title", "Lightbulb Desktop")
        .enabled(false)
        .build(app)?;
    let status = MenuItemBuilder::with_id("status", status_label)
        .enabled(false)
        .build(app)?;
    let quit = MenuItemBuilder::with_id("quit", "Quit").build(app)?;

    let menu = MenuBuilder::new(app)
        .item(&title)
        .separator()
        .item(&status)
        .separator()
        .item(&quit)
        .build()?;

    // Update existing tray or create new one
    if let Some(tray) = app.tray_by_id("main") {
        tray.set_menu(Some(menu))?;
    } else {
        TrayIconBuilder::with_id("main")
            .tooltip("Lightbulb")
            .menu(&menu)
            .on_menu_event(|app, event| {
                if event.id().as_ref() == "quit" {
                    app.exit(0);
                }
            })
            .build(app)?;
    }

    Ok(())
}
