-- 1.0.7-2
local home = os.getenv("HOME")
local name_file_path = home .. "/.yradio/tmp/nowplay"
local conf_file_path = home .. "/.config/streamrip.conf"

local station_name = "Recordings"
local output_dir = ""

local track_counter = 1
local last_fingerprint = ""
local last_recorded_path = nil
local has_icy_metadata = false

-- Minimale bestandsgrootte in bytes (25 KB).
-- Kleinere bestanden (zoals snelle metadata-correcties) worden direct verwijderd en overschreven.
local MIN_VALID_BYTES = 25 * 1024

-- Verberg de afspeeltijd/buffer-teller in de terminal
mp.set_property("term-status-msg", "")

-- Maakt een "vingerafdruk" van de titel (alleen kleine letters en cijfers) om dubbele triggers te herkennen
local function get_fingerprint(str)
    return str:lower():gsub("[^%w]", "")
end

-- Hulpfunctie om de grootte van een bestand op te vragen
local function get_file_size(path)
    if not path then return 0 end
    local f = io.open(path, "rb")
    if not f then return 0 end
    local size = f:seek("end")
    f:close()
    return size or 0
end

-- Reageert direct (0 ms vertraging) op metadata wijzigingen
function on_metadata_change(source, title)
    if not title or title == "" then return end

    -- Als deze stream ICY-metadata gebruikt, negeer dan updates van media-title
    if source == "icy" then
        has_icy_metadata = true
    elseif source == "media-title" and has_icy_metadata then
        return
    end

    local final_filename = title

    -- Streams zonder goede metadata krijgen de stationsnaam
    if title == "stream" or title:find("remix") or title:find("radiobrowser") then
        final_filename = station_name
    end

    -- 1. Maak de titel schoon voor de bestandsnaam (inclusief backticks)
    local safe_title = final_filename:gsub("[%c%?%*\\/<>|\"':`]", "_"):gsub("_+", "_"):gsub("^[%s_]+", ""):gsub("[%.%s_]+$", "")
    if safe_title == "" then return end

    -- 2. Vergelijk de vingerafdruk (negeert underscores, spaties en leestekens)
    local fingerprint = get_fingerprint(safe_title)
    if fingerprint == "" or fingerprint == last_fingerprint then return end
    last_fingerprint = fingerprint

    -- 3. Check of het vorige bestand onder de 25 KB is (bijv. door een directe metadata-correctie)
    if last_recorded_path and track_counter > 1 then
        mp.set_property("stream-record", "")
        local prev_size = get_file_size(last_recorded_path)

        if prev_size < MIN_VALID_BYTES then
            print(" Replacing incomplete file (" .. math.floor(prev_size / 1024) .. " KB): " .. last_recorded_path)
            os.remove(last_recorded_path)
            track_counter = track_counter - 1
        end
    end

    -- 4. Dynamische extensie detectie
    local ext = mp.get_property("file-format")
    local codec = mp.get_property("audio-codec-name")

    if ext == "hls" or ext == "lavf" or not ext or ext == "" then
        if codec == "aac" then
            ext = "aac"
        elseif codec == "mp3" then
            ext = "mp3"
        elseif codec == "vorbis" or codec == "opus" then
            ext = "ogg"
        else
            ext = "ts"
        end
    end

    local numbered_title = string.format("%03d - %s", track_counter, safe_title)
    local full_path = output_dir .. numbered_title .. "." .. ext

    print(" Recording [" .. string.format("%03d", track_counter) .. "] (" .. ext .. ") -> " .. full_path)

    if not mp.set_property("stream-record", full_path) then
        mp.set_property("record-file", full_path)
    end

    last_recorded_path = full_path
    track_counter = track_counter + 1
end

local function start_ripping_after_delay()
    print(" Initialising... ")

    -- 1. Probeer de stationsnaam uit nowplay te lezen
    local file = io.open(name_file_path, "r")
    if file then
        local first_line = file:read("*l")
        file:close()
        if first_line and first_line ~= "" then
            station_name = first_line:gsub("^%s*(.-)%s*$", "%1"):gsub("[%c%?%*\\/<>|\"':]", "_")
        end
    end

    -- 2. Lees de basismap uit streamrip.conf
    local base_dir = home
    local conf_file = io.open(conf_file_path, "r")
    if conf_file then
        local conf_line = conf_file:read("*l")
        conf_file:close()
        if conf_line and conf_line ~= "" then
            local cleaned_path = conf_line:gsub("^%s*(.-)%s*$", "%1"):gsub("/+$", "")
            if cleaned_path ~= "" then
                base_dir = cleaned_path
            end
        end
    end

    -- 3. Definieer en maak de output map aan
    output_dir = base_dir .. "/" .. station_name .. "/"
    mp.command_native({
        name = "subprocess",
        args = { "mkdir", "-p", output_dir },
        playback_only = false
    })

    -- Luister naar ICY-tags (primaire bron)
    mp.observe_property("filtered-metadata", "native", function(_, meta)
        if meta then
            local title = meta["icy-title"] or meta["title"] or meta["StreamTitle"]
            if title then
                on_metadata_change("icy", title)
            end
        end
    end)

    -- Fallback voor streams die alleen media-title gebruiken
    mp.observe_property("media-title", "string", function(_, title)
        on_metadata_change("media-title", title)
    end)
end

print(" Waiting... ")
mp.add_timeout(3.0, start_ripping_after_delay)
