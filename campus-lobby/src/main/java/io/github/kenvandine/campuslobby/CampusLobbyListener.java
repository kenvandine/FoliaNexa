package io.github.kenvandine.campuslobby;

import org.bukkit.Bukkit;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerJoinEvent;

/**
 * A themed welcome message on join. Location-tied work goes through
 * Bukkit.getRegionScheduler() against the player's own location — see
 * SceneBuilder's javadoc / docs/plugin-dev/02-plugin-architecture.md §2.3
 * in the folia-server repo for why this can't use the legacy
 * getServer().getScheduler() API under Folia.
 */
public final class CampusLobbyListener implements Listener {
    private final CampusLobbyPlugin plugin;

    public CampusLobbyListener(CampusLobbyPlugin plugin) {
        this.plugin = plugin;
    }

    @EventHandler
    public void onJoin(PlayerJoinEvent event) {
        Player player = event.getPlayer();
        Bukkit.getRegionScheduler().runDelayed(plugin, player.getLocation(), task -> {
            if (player.isOnline()) {
                player.sendMessage("Welcome to NC State's Wolfpack lobby — Go Pack!");
            }
        }, 20L);
    }
}
