package dev.foliasmp.routessync;

import com.google.inject.Inject;
import com.velocitypowered.api.event.Subscribe;
import com.velocitypowered.api.event.proxy.ProxyInitializeEvent;
import com.velocitypowered.api.event.player.PlayerChooseInitialServerEvent;
import com.velocitypowered.api.plugin.Plugin;
import com.velocitypowered.api.proxy.ProxyServer;
import com.velocitypowered.api.proxy.server.RegisteredServer;
import com.velocitypowered.api.proxy.server.ServerInfo;
import org.slf4j.Logger;

import java.net.InetSocketAddress;
import java.time.Duration;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Keeps Velocity's backend server list in sync with folia-smp-mgmt's
 * {@code GET /api/v1/routes}, so worlds can come and go without a proxy
 * restart. PLAN.md §7.
 *
 * Configuration is via environment variables — matching how the rest of
 * this project's snaps are configured (mgmt, node), and avoiding a second
 * config-file format for what's fundamentally the same three settings:
 *
 * <ul>
 *   <li>{@code FOLIA_MGMT_URL} (required) — e.g. https://mgmt.internal:8443</li>
 *   <li>{@code FOLIA_MGMT_API_TOKEN} (required) — an mgmt API token, any role (§11A) works; only reads /api/v1/routes</li>
 *   <li>{@code FOLIA_ROUTES_POLL_SECONDS} (optional, default 5)</li>
 * </ul>
 */
@Plugin(
        id = "folia-routes-sync",
        name = "Folia Routes Sync",
        version = "0.1.0",
        description = "Syncs Velocity's backend list with folia-smp-mgmt's live routing table"
)
public final class FoliaRoutesSyncPlugin {

    private final ProxyServer server;
    private final Logger logger;
    private final AtomicReference<String> defaultWorld = new AtomicReference<>();

    @Inject
    public FoliaRoutesSyncPlugin(ProxyServer server, Logger logger) {
        this.server = server;
        this.logger = logger;
    }

    @Subscribe
    public void onProxyInitialize(ProxyInitializeEvent event) {
        String mgmtUrl = requireEnv("FOLIA_MGMT_URL");
        String apiToken = requireEnv("FOLIA_MGMT_API_TOKEN");
        int pollSeconds = Integer.parseInt(System.getenv().getOrDefault("FOLIA_ROUTES_POLL_SECONDS", "5"));

        MgmtRoutesClient client = new MgmtRoutesClient(mgmtUrl, apiToken, Duration.ofSeconds(10));

        logger.info("polling {} every {}s for the live routing table", mgmtUrl, pollSeconds);

        server.getScheduler()
                .buildTask(this, () -> pollAndReconcile(client))
                .repeat(Duration.ofSeconds(pollSeconds))
                .schedule();
    }

    @Subscribe
    public void onChooseInitialServer(PlayerChooseInitialServerEvent event) {
        String world = defaultWorld.get();
        if (world == null) {
            return;
        }
        server.getServer(world).ifPresent(event::setInitialServer);
    }

    private void pollAndReconcile(MgmtRoutesClient client) {
        List<Route> desired;
        try {
            desired = client.fetch();
        } catch (Exception e) {
            logger.warn("failed to fetch routes from mgmt, keeping current server list: {}", e.getMessage());
            return;
        }

        Map<String, String> currentlyRegistered = new HashMap<>();
        for (RegisteredServer registered : server.getAllServers()) {
            ServerInfo info = registered.getServerInfo();
            currentlyRegistered.put(info.getName(), addressOf(info));
        }

        RouteDiff.Plan plan = RouteDiff.compute(currentlyRegistered, desired);

        for (Route route : plan.toRegister()) {
            // Velocity keys registration by name and won't let us just
            // overwrite an existing one in place, so an address change
            // (world moved hosts) needs the stale entry gone first.
            server.getServer(route.world()).ifPresent(existing -> server.unregisterServer(existing.getServerInfo()));
            server.registerServer(new ServerInfo(route.world(), new InetSocketAddress(route.host(), route.port())));
            logger.info("registered '{}' -> {}", route.world(), route.address());
        }

        for (String name : plan.toUnregister()) {
            server.getServer(name).ifPresent(rs -> {
                server.unregisterServer(rs.getServerInfo());
                logger.info("unregistered '{}' (no longer in mgmt's route table)", name);
            });
        }

        plan.defaultWorld().ifPresent(defaultWorld::set);
    }

    private static String addressOf(ServerInfo info) {
        InetSocketAddress addr = info.getAddress();
        return addr.getHostString() + ":" + addr.getPort();
    }

    private static String requireEnv(String name) {
        String value = System.getenv(name);
        if (value == null || value.isBlank()) {
            throw new IllegalStateException(name + " must be set (see snapcraft.yaml environment)");
        }
        return value;
    }
}
