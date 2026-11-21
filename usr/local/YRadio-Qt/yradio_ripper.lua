local home = os.getenv("HOME")
local name_file_path = home .. "/.yradio/tmp/nowplay"
local conf_file_path = home .. "/.config/streamrip.conf"

-- We maken deze variabelen globaal binnen het script zodat de functies erbij kunnen
local station_name = "Recordings"
local output_dir = ""

-- NIEUW: Globale teller voor de nummers en een variabele om dubbele triggers te voorkomen
local track_counter = 1
local last_title = ""

-- De functie die reageert op metadata (nummers) wijzigingen
function on_metadata_change(name, title)
    -- Als er helemaal geen titel is, stop dan
    if not title or title == "" then return end
    
    -- Voorkom dat hetzelfde nummer twee keer achter elkaar een nieuw bestand opent
    if title == last_title then return end
    last_title = title

    local final_filename = title

    -- HIER GEBEURT DE MAGIE VOOR STREAMS ZONDER METADATA:
    if title == "stream" or title:find("remix") or title:find("radiobrowser") then
        final_filename = station_name
    end

    -- Maak de definitieve titel veilig voor bestandsnamen
    local safe_title = final_filename:gsub("[%c%?%*\\/<>|\"':]", "_"):gsub("[%.%s]+$", "")
    
    -- DYNAMISCHE EXTENSIE DETECTIE:
    -- Vraag aan mpv wat het echte formaat van de inkomende stream is (bijv. mp3, aac, ogg)
    local ext = mp.get_property("file-format")
    if not ext or ext == "" then
        ext = "mp3" -- Veilige fallback als mpv het nog niet weet
    end

    -- AANGEPAST: Voeg de teller toe aan de bestandsnaam (bijv. 001 - Titel.mp3)
    local numbered_title = string.format("%03d - %s", track_counter, safe_title)
    
    -- Bepaal het volledige pad met de genummerde titel en juiste extensie
    local full_path = output_dir .. numbered_title .. "." .. ext

    print(" Recording [" .. string.format("%03d", track_counter) .. "] (" .. ext .. ") -> " .. full_path)

    -- Probeer eerst de moderne property te zetten; als dat faalt, gebruik de oude
    if not mp.set_property("stream-record", full_path) then
        mp.set_property("record-file", full_path)
    end
    
    -- NIEUW: Verhoog de teller voor het volgende nummer
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
            -- Haal eventuele spaties aan het begin/eind weg en maak veilig voor een mapnaam
            station_name = first_line:gsub("^%s*(.-)%s*$", "%1"):gsub("[%c%?%*\\/<>|\"':]", "_")

        end
    end

    -- 2. Lees de basismap uit streamrip.conf (val terug op $HOME als het bestand niet bestaat/leeg is)
    local base_dir = home
    local conf_file = io.open(conf_file_path, "r")
    if conf_file then
        local conf_line = conf_file:read("*l")
        conf_file:close()
        if conf_line and conf_line ~= "" then
            -- Verwijder spaties/newlines en een eventuele laatste slash (bijv. /mnt/sda2/ -> /mnt/sda2)
            local cleaned_path = conf_line:gsub("^%s*(.-)%s*$", "%1"):gsub("/+$", "")
            if cleaned_path ~= "" then
                base_dir = cleaned_path
            end
        end
    end

    -- 3. Definieer de output map op basis van de basismap en stationsnaam
    output_dir = base_dir .. "/" .. station_name .. "/"

    -- 4. Maak de map fysiek aan op je schijf
    mp.command_native({
        name = "subprocess",
        args = { "mkdir", "-p", output_dir },
        playback_only = false
    })

    -- 5. Start NU pas het observeren van de metadata
    -- Dit triggert automatisch AL DE EERSTE KEER met de huidige waarde!
    mp.observe_property("media-title", "string", on_metadata_change)
end

-- HIER WORDT DE SLEEP 5 INITIEEL AANGEROEPEN:
print(" Waiting... ")
mp.add_timeout(5.0, start_ripping_after_delay)
