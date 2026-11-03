use axum::{
    body::{Body, to_bytes},
    http::{Request, StatusCode, header},
};
use serde_json::{Value, json};
use tempfile::TempDir;
use tower::ServiceExt;

use super::*;

async fn fixture() -> (TempDir, AppState) {
    let dir = tempfile::tempdir().unwrap();
    let state = AppState::new(Some(dir.path().join("nested/config.json")))
        .await
        .unwrap();
    state.set_authority("127.0.0.1:32123".into()).await;
    (dir, state)
}

fn request(method: &str, uri: &str, body: Option<Value>) -> Request<Body> {
    let mut builder = Request::builder()
        .method(method)
        .uri(uri)
        .header(header::HOST, "127.0.0.1:32123");
    if body.is_some() {
        builder = builder.header(header::CONTENT_TYPE, "application/json");
    }
    builder
        .body(
            body.map(|v| Body::from(v.to_string()))
                .unwrap_or_else(Body::empty),
        )
        .unwrap()
}

async fn json(response: axum::response::Response) -> Value {
    serde_json::from_slice(
        &to_bytes(response.into_body(), 2 * MAX_JSON_BYTES)
            .await
            .unwrap(),
    )
    .unwrap()
}

#[tokio::test]
async fn embedded_ui_and_security_headers_are_present() {
    let (_dir, state) = fixture().await;
    let response = router(state)
        .oneshot(request("GET", "/", None))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(response.headers()[header::CACHE_CONTROL], "no-store");
    assert_eq!(
        response.headers()[header::X_CONTENT_TYPE_OPTIONS],
        "nosniff"
    );
    assert_eq!(response.headers()["x-frame-options"], "DENY");
    assert_eq!(response.headers()["referrer-policy"], "no-referrer");
    assert!(response.headers().contains_key("content-security-policy"));
    let text = String::from_utf8(
        to_bytes(response.into_body(), 4 * MAX_JSON_BYTES)
            .await
            .unwrap()
            .to_vec(),
    )
    .unwrap();
    assert!(!text.contains("/*__STYLE__*/"));
    assert!(!text.contains("/*__SCRIPT__*/"));
    assert!(text.contains("/api/status"));
}

#[tokio::test]
async fn startup_is_disarmed_and_config_is_saved_atomically() {
    let (dir, state) = fixture().await;
    let app = router(state.clone());
    let response = app
        .clone()
        .oneshot(request("GET", "/api/status", None))
        .await
        .unwrap();
    let status = json(response).await;
    assert_eq!(status["controller"]["armed"], false);
    assert_eq!(status["controller"]["output"]["a"], 0);
    assert_eq!(status["controller"]["output"]["b"], 0);

    let response = app
        .oneshot(request(
            "POST",
            "/api/config",
            Some(json!({"device":{"kind":"mock"},"safety":{"max_pct":17.0}})),
        ))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let saved: Value = serde_json::from_slice(
        &tokio::fs::read(dir.path().join("nested/config.json"))
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(saved["device"]["kind"], "mock");
    assert_eq!(saved["safety"]["max_pct"], 17.0);
    let files = std::fs::read_dir(dir.path().join("nested"))
        .unwrap()
        .count();
    assert_eq!(files, 1, "atomic temporary file must not remain");
}

#[tokio::test]
async fn actions_enforce_manual_arm_and_trip_zeros_output() {
    let (_dir, state) = fixture().await;
    let app = router(state);
    let patch = request(
        "POST",
        "/api/config",
        Some(json!({"device":{"kind":"mock"}})),
    );
    assert_eq!(
        app.clone().oneshot(patch).await.unwrap().status(),
        StatusCode::OK
    );
    let arm = app
        .clone()
        .oneshot(request(
            "POST",
            "/api/actions",
            Some(json!({"action":"arm"})),
        ))
        .await
        .unwrap();
    assert_eq!(arm.status(), StatusCode::BAD_REQUEST);
    assert_eq!(
        app.clone()
            .oneshot(request(
                "POST",
                "/api/actions",
                Some(json!({"action":"start"}))
            ))
            .await
            .unwrap()
            .status(),
        StatusCode::OK
    );
    assert_eq!(
        app.clone()
            .oneshot(request(
                "POST",
                "/api/actions",
                Some(json!({"action":"arm"}))
            ))
            .await
            .unwrap()
            .status(),
        StatusCode::OK
    );
    assert_eq!(
        app.clone()
            .oneshot(request(
                "POST",
                "/api/actions",
                Some(json!({"action":"test_pulse","pct":5,"ms":200}))
            ))
            .await
            .unwrap()
            .status(),
        StatusCode::OK
    );
    assert_eq!(
        app.clone()
            .oneshot(request(
                "POST",
                "/api/actions",
                Some(json!({"action":"trip"}))
            ))
            .await
            .unwrap()
            .status(),
        StatusCode::OK
    );
    let status = json(
        app.oneshot(request("GET", "/api/status", None))
            .await
            .unwrap(),
    )
    .await;
    assert_eq!(status["controller"]["armed"], false);
    assert_eq!(status["controller"]["output"]["a"], 0);
    assert_eq!(status["controller"]["output"]["b"], 0);
}

#[tokio::test]
async fn rejects_cross_site_bad_host_origin_and_oversize_json() {
    let (_dir, state) = fixture().await;
    let app = router(state);
    let bad_host = Request::builder()
        .uri("/api/status")
        .header(header::HOST, "evil.example")
        .body(Body::empty())
        .unwrap();
    assert_eq!(
        app.clone().oneshot(bad_host).await.unwrap().status(),
        StatusCode::FORBIDDEN
    );
    let bad_origin = Request::builder()
        .uri("/api/status")
        .header(header::HOST, "127.0.0.1:32123")
        .header(header::ORIGIN, "https://evil.example")
        .body(Body::empty())
        .unwrap();
    assert_eq!(
        app.clone().oneshot(bad_origin).await.unwrap().status(),
        StatusCode::FORBIDDEN
    );
    let cross = Request::builder()
        .uri("/api/status")
        .header(header::HOST, "127.0.0.1:32123")
        .header("sec-fetch-site", "cross-site")
        .body(Body::empty())
        .unwrap();
    assert_eq!(
        app.clone().oneshot(cross).await.unwrap().status(),
        StatusCode::FORBIDDEN
    );
    let huge = Request::builder()
        .method("POST")
        .uri("/api/config")
        .header(header::HOST, "127.0.0.1:32123")
        .header(header::CONTENT_TYPE, "application/json")
        .body(Body::from(format!(
            "{{\"x\":\"{}\"}}",
            "x".repeat(MAX_JSON_BYTES)
        )))
        .unwrap();
    assert_eq!(
        app.oneshot(huge).await.unwrap().status(),
        StatusCode::PAYLOAD_TOO_LARGE
    );
}

#[tokio::test]
async fn waves_and_sources_match_frontend_shape() {
    let (_dir, state) = fixture().await;
    let app = router(state);
    let sources = json(
        app.clone()
            .oneshot(request("GET", "/api/sources", None))
            .await
            .unwrap(),
    )
    .await;
    assert_eq!(sources["ok"], true);
    assert!(sources["engine_source"].is_string());
    assert!(sources["catalog"].is_array());
    assert!(sources["enabled"].is_array());
    assert!(sources["runtime"].is_array());
    assert!(sources.get("http").is_none());   // 0.7.0 移除了 HTTP 事件源
    let waves = json(
        app.oneshot(request("GET", "/api/waves", None))
            .await
            .unwrap(),
    )
    .await;
    assert_eq!(waves["ok"], true);
    assert!(waves["presets"].is_array());
    assert!(waves["waves"].is_array());
    assert!(waves["rules"].is_object());
    assert!(waves["text"].is_string());
    assert!(waves["freq_min"].is_number());
    assert!(waves["freq_max"].is_number());
    assert!(waves["unit_ms"].is_number());
    assert_eq!(waves["unit_chars"], 16);
}

#[tokio::test]
async fn update_endpoint_is_check_only_and_cache_can_be_cleared() {
    let (_dir, state) = fixture().await;
    let app = router(state);
    let initial = json(
        app.clone()
            .oneshot(request("GET", "/api/update", None))
            .await
            .unwrap(),
    )
    .await;
    assert!(initial["current"].is_string());
    assert!(initial["ok"].is_null());

    let response = app
        .oneshot(request("POST", "/api/update", Some(json!({"clear": true}))))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let cleared = json(response).await;
    assert!(cleared["current"].is_string());
    assert!(cleared["ok"].is_null());
}

#[tokio::test]
async fn vision_surfaces_idle_source_and_rejects_bad_delay() {
    let (_dir, state) = fixture().await;
    let app = router(state);

    let idle = json(
        app.clone()
            .oneshot(request("GET", "/api/vision", None))
            .await
            .unwrap(),
    )
    .await;
    assert_eq!(idle["ok"], true);
    assert_eq!(idle["active"], false);
    assert_eq!(idle["alive"], false);
    assert_eq!(idle["detail"], "视觉源未启动");
    assert!(idle["fps"].is_number());
    assert!(idle["configured"].is_null() || idle["configured"].is_object());

    // 越界的 delay_s 必须在碰窗口之前就被拒绝，否则会把校准拖进抓屏路径。
    let response = app
        .clone()
        .oneshot(request(
            "POST",
            "/api/vision/calibrate",
            Some(json!({"delay_s": 99})),
        ))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    assert!(json(response).await["error"].as_str().is_some());

    let status = json(
        app.clone()
            .oneshot(request("GET", "/api/status", None))
            .await
            .unwrap(),
    )
    .await;
    let vision = &status["controller"]["vision"];
    assert_eq!(vision["alive"], false);
    assert_eq!(vision["hp"], Value::Null);
    let entry = |body: &Value| -> Option<Value> {
        body["controller"]["sources"]
            .as_array()
            .unwrap()
            .iter()
            .find(|item| item["name"] == "vision")
            .cloned()
    };
    // 默认只启用 game_bridge，vision 不该混进"已启用源"列表。
    assert!(entry(&status).is_none());

    let enabled = app
        .clone()
        .oneshot(request(
            "POST",
            "/api/config",
            Some(json!({"sources": {"enabled": ["vision"]}})),
        ))
        .await
        .unwrap();
    assert_eq!(enabled.status(), StatusCode::OK);
    let status = json(
        app.clone()
            .oneshot(request("GET", "/api/status", None))
            .await
            .unwrap(),
    )
    .await;
    let entry = entry(&status).expect("启用后 vision 应出现在 sources 里");
    assert_eq!(entry["alive"], false);
    assert_eq!(entry["label"], "外部 HUD 视觉（EAC 兼容）");

    let sources = json(
        app.oneshot(request("GET", "/api/sources", None))
            .await
            .unwrap(),
    )
    .await;
    assert!(
        sources["catalog"]
            .as_array()
            .unwrap()
            .iter()
            .any(|item| item["name"] == "vision")
    );
    assert_eq!(sources["vision"]["active"], false);
}

#[tokio::test]
async fn unimplemented_sources_report_null_alive() {
    // Rust 客户端只接了 vision。game_bridge / udp 的名字可以留在配置里，
    // 但它们在本进程没有采集端——绝不能用 running 冒充存活。
    let (_dir, state) = fixture().await;
    let app = router(state);

    let status = json(
        app.clone()
            .oneshot(request("GET", "/api/status", None))
            .await
            .unwrap(),
    )
    .await;
    let entries = status["controller"]["sources"].as_array().unwrap();
    let bridge = entries
        .iter()
        .find(|entry| entry["name"] == "game_bridge")
        .expect("默认配置启用 game_bridge");
    assert_eq!(bridge["implemented"], false);
    assert_eq!(bridge["alive"], Value::Null);

    let sources = json(
        app.oneshot(request("GET", "/api/sources", None))
            .await
            .unwrap(),
    )
    .await;
    assert_eq!(sources["implemented"], json!(["vision"]));
    let bridge = sources["runtime"]
        .as_array()
        .unwrap()
        .iter()
        .find(|entry| entry["name"] == "game_bridge")
        .cloned()
        .expect("runtime 里应有 game_bridge");
    assert_eq!(bridge["implemented"], false);
    assert_eq!(bridge["alive"], Value::Null);
    assert_eq!(bridge["running"], false);
}

#[tokio::test]
async fn vision_config_is_validated() {
    let (_dir, state) = fixture().await;
    let app = router(state);
    for patch in [
        json!({"vision": {"fps": 0}}),
        json!({"vision": {"fps": 31}}),
        json!({"vision": {"brightness": 0}}),
        json!({"vision": {"stale_timeout_s": 0.1}}),
        json!({"vision": {"hp_bar": {"x": -4, "y": 0, "w": 100, "h": 8}}}),
    ] {
        let response = app
            .clone()
            .oneshot(request("POST", "/api/config", Some(patch)))
            .await
            .unwrap();
        assert_eq!(
            response.status(),
            StatusCode::BAD_REQUEST,
            "应当拒绝非法视觉配置"
        );
    }

    let ok = app
        .oneshot(request(
            "POST",
            "/api/config",
            Some(json!({"vision": {"fps": 15, "brightness": 150}})),
        ))
        .await
        .unwrap();
    assert_eq!(ok.status(), StatusCode::OK);
    let body = json(ok).await;
    assert_eq!(body["ok"], true);
}
