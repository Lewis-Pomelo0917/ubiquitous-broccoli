#[cfg(desktop)]
use tauri::Manager;

#[tauri::command]
fn read_ai_feed() -> Result<String, String> {
    let executable = std::env::current_exe()
        .map_err(|error| format!("无法定位应用程序：{error}"))?;
    let directory = executable
        .parent()
        .ok_or_else(|| "无法定位应用程序目录".to_string())?;
    let feed_path = directory.join("ai-intelligence.json");
    std::fs::read_to_string(&feed_path)
        .map_err(|error| format!("无法读取本地 AI 情报（{}）：{error}", feed_path.display()))
}

#[tauri::command]
fn hide_to_tray(window: tauri::WebviewWindow) -> Result<(), String> {
    window.hide().map_err(|error| format!("无法最小化到托盘：{error}"))
}

#[tauri::command]
fn exit_application(app: tauri::AppHandle) {
    app.exit(0);
}

#[cfg(desktop)]
fn show_main_window(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

#[cfg(desktop)]
fn setup_tray(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    use tauri::{
        menu::{Menu, MenuItem},
        tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    };

    let open_item = MenuItem::with_id(app, "open", "打开 Cubic", true, None::<&str>)?;
    let quit_item = MenuItem::with_id(app, "quit", "退出程序", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open_item, &quit_item])?;
    let icon = app
        .default_window_icon()
        .cloned()
        .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::NotFound, "正式应用图标不可用"))?;

    TrayIconBuilder::with_id("main")
        .icon(icon)
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "open" => show_main_window(app),
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_main_window(tray.app_handle());
            }
        })
        .build(app)?;

    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let mut builder = tauri::Builder::default();

    #[cfg(desktop)]
    {
        builder = builder.plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show_main_window(app);
        }));
    }

    builder = builder
        .invoke_handler(tauri::generate_handler![read_ai_feed, hide_to_tray, exit_application]);

    #[cfg(desktop)]
    {
        builder = builder.setup(|app| setup_tray(app));
    }

    builder
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
