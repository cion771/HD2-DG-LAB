use std::{
    net::{IpAddr, Ipv4Addr, SocketAddr},
    path::PathBuf,
};

use clap::{Parser, Subcommand};
use hd2_app::{AppState, default_config_path, router};
use tracing_subscriber::EnvFilter;

#[derive(Debug, Parser)]
#[command(name = "hd2-app", version, about = "HD2 · DG-LAB loopback controller")]
struct Cli {
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Run the local API and open the desktop UI.
    Desktop(ServerArgs),
    /// Run the local API and open its browser UI.
    Ui(ServerArgs),
    /// Run the local API without opening a browser.
    Run(ServerArgs),
    /// Validate configuration and print local diagnostics.
    Doctor(CommonArgs),
    /// Print the configured wave library as JSON.
    Waves(CommonArgs),
    /// Check GitHub for a newer release.
    Update(CommonArgs),
}

#[derive(Debug, clap::Args, Clone)]
struct CommonArgs {
    /// Override the configuration path.
    #[arg(long)]
    config: Option<PathBuf>,
}

#[derive(Debug, clap::Args, Clone)]
struct ServerArgs {
    #[command(flatten)]
    common: CommonArgs,
    /// Loopback port (0 chooses a free port).
    #[arg(long, default_value_t = 0)]
    port: u16,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| "hd2_app=info".into()),
        )
        .init();
    let cli = Cli::parse();
    match cli.command.unwrap_or(Command::Desktop(ServerArgs {
        common: CommonArgs { config: None },
        port: 0,
    })) {
        Command::Desktop(args) | Command::Ui(args) => serve(args, true).await?,
        Command::Run(args) => serve(args, false).await?,
        Command::Doctor(args) => doctor(args).await?,
        Command::Waves(args) => waves(args).await?,
        Command::Update(args) => update(args).await?,
    }
    Ok(())
}

async fn serve(args: ServerArgs, open: bool) -> Result<(), Box<dyn std::error::Error>> {
    let state = AppState::new(args.common.config).await?;
    let listener =
        tokio::net::TcpListener::bind(SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), args.port))
            .await?;
    let addr = listener.local_addr()?;
    state.set_authority(addr.to_string()).await;
    let url = format!("http://{addr}/");
    println!("HD2 local API: {url}");
    if open {
        if let Err(error) = webbrowser::open(&url) {
            tracing::warn!(%error, "could not open the browser");
        }
    }
    let notifier = state.shutdown_notifier();
    let stop_state = state.clone();
    let server = axum::serve(listener, router(state)).with_graceful_shutdown(async move {
        tokio::select! {
            _ = notifier.notified() => {},
            _ = tokio::signal::ctrl_c() => {},
        }
        stop_state.safe_shutdown().await;
    });
    server.await?;
    Ok(())
}

async fn doctor(args: CommonArgs) -> Result<(), Box<dyn std::error::Error>> {
    let path = args.config.unwrap_or_else(default_config_path);
    let state = AppState::new(Some(path.clone())).await?;
    let cfg = state.config_snapshot().await;
    println!("config: {}", path.display());
    println!(
        "device: {} {}:{}",
        cfg.device.kind, cfg.device.host, cfg.device.port
    );
    println!("sources: {}", cfg.sources.enabled.join(", "));
    println!("startup: DISARMED (safe)");
    Ok(())
}

async fn waves(args: CommonArgs) -> Result<(), Box<dyn std::error::Error>> {
    let state = AppState::new(args.config).await?;
    println!(
        "{}",
        serde_json::to_string_pretty(&state.waves_snapshot().await)?
    );
    Ok(())
}

async fn update(args: CommonArgs) -> Result<(), Box<dyn std::error::Error>> {
    let state = AppState::new(args.config).await?;
    println!(
        "{}",
        serde_json::to_string_pretty(&state.check_update().await?)?
    );
    Ok(())
}
