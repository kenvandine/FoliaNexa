package __PACKAGE__;

import org.bukkit.Bukkit;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerJoinEvent;

/**
 * Example listener showing the Folia-safe scheduling pattern: work tied
 * to a specific player/location goes through Bukkit.getRegionScheduler(),
 * scheduled against that player's location — NOT
 * getServer().getScheduler().runTaskLater(...), which has no defined
 * thread to run on under Folia's regionized ticking. See
 * docs/plugin-dev/02-plugin-architecture.md §2.3 in the folia-server
 * repo for the full explanation and the other two schedulers
 * (GlobalRegionScheduler for non-location-tied work, AsyncScheduler for
 * I/O that touches no Bukkit API at all).
 */
public final class __LISTENER_CLASS__ implements Listener {
    private final __MAIN_CLASS_SIMPLE__ plugin;

    public __LISTENER_CLASS__(__MAIN_CLASS_SIMPLE__ plugin) {
        this.plugin = plugin;
    }

    @EventHandler
    public void onJoin(PlayerJoinEvent event) {
        Player player = event.getPlayer();
        // Re-check player.isOnline() inside the callback — by the time a
        // delayed task fires, they may have logged off or moved to a
        // different region than the one this was scheduled against.
        Bukkit.getRegionScheduler().runDelayed(plugin, player.getLocation(), task -> {
            if (player.isOnline()) {
                // TODO: replace with __PLUGIN_NAME__'s actual join behavior.
                player.sendMessage("Welcome!");
            }
        }, 20L); // ticks — 20 ticks/sec, so this is a 1-second delay
    }
}
