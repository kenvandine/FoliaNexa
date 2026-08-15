package io.github.kenvandine.campuslobby;

import org.bukkit.Location;
import org.bukkit.command.Command;
import org.bukkit.command.CommandExecutor;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Player;

public final class CampusLobbyCommand implements CommandExecutor {
    private final CampusLobbyPlugin plugin;

    public CampusLobbyCommand(CampusLobbyPlugin plugin) {
        this.plugin = plugin;
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (args.length == 1 && args[0].equalsIgnoreCase("reload") && sender.hasPermission("campuslobby.reload")) {
            plugin.reloadConfig();
            sender.sendMessage("CampusLobby config reloaded.");
            return true;
        }

        if (args.length == 1 && args[0].equalsIgnoreCase("build") && sender.hasPermission("campuslobby.build")) {
            Location origin = (sender instanceof Player player)
                    ? player.getLocation()
                    : plugin.getServer().getWorlds().get(0).getSpawnLocation();

            SceneConfig config = ConfigLoader.load(plugin.getConfig());
            SceneBuilder.build(plugin, origin.getWorld(), origin, config);

            sender.sendMessage("Building the NC State Wolfpack lobby scene at "
                    + origin.getBlockX() + ", " + origin.getBlockY() + ", " + origin.getBlockZ()
                    + " — it's placed chunk-by-chunk, give it a few seconds to finish.");
            return true;
        }

        sender.sendMessage("Usage: /campuslobby <build|reload>");
        return true;
    }
}
