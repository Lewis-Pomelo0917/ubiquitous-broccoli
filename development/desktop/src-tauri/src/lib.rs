#[cfg(desktop)]
use tauri::Manager;

mod ai_watch;

#[tauri::command]
fn read_ai_feed(app: tauri::AppHandle) -> Result<String, String> {
    ai_watch::read_ai_feed(&app)
}

#[tauri::command]
fn read_ai_status(app: tauri::AppHandle) -> Result<String, String> {
    ai_watch::read_ai_status(&app)
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
    builder = builder.manage(ai_watch::AiWatchState::default());

    #[cfg(desktop)]
    {
        builder = builder.plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show_main_window(app);
        }));
    }

    builder = builder.invoke_handler(tauri::generate_handler![
        read_ai_feed,
        read_ai_status,
        hide_to_tray,
        exit_application
    ]);

    #[cfg(desktop)]
    {
        builder = builder.setup(|app| {
            setup_tray(app)?;
            ai_watch::start(app.handle().clone()).map_err(std::io::Error::other)?;
            Ok(())
        });
    }

    let app = builder
        .build(tauri::generate_context!())
        .expect("error while building tauri application");
    app.run(|app, event| {
        if matches!(event, tauri::RunEvent::Exit) {
            ai_watch::shutdown(&app);
        }
    });
}
