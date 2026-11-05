# Reprise

Reprise is a review-first, self-hosted media analysis application built on Media Preview Generator and Recurring Content Detector research. It finds repeated material and lets you protect unique scenes before approving skips. Detection is a suggestion, not an understanding of the story: tiny visual edits and quiet or brief changes can be missed. Plex writes are off by default.

## Review your library

Start in the library-first interface: connect a Plex server, refresh a library, choose an episode and its exact media version, and explicitly request analysis. Refresh indexes the library; it does not implicitly start analysis. Analysis, video comparison previews and refresh run as queued jobs with pause, resume and cancel controls. CPU-only operation is supported and no language model is required; a GPU passed to the container decodes the analysed video when it checks out ([Optional GPU decoding](#optional-gpu-decoding)).

Detection proposes ranges; it does not approve them. Inspect the evidence and original video, adjust keep and skip ranges, and approve or lock decisions yourself. A keep range vetoes overlapping skips; eligible skips remain separate rather than being joined across protected material. Native signal matching tolerates encoding differences and does not guarantee that every unique scene is found. Uncertain or changed evidence is not approval.

The [playback API](docs/reprise-api.md) serves saved results using the exact Plex playback identity, including the selected audio stream. Missing, stale, ambiguous or unapproved results yield no eligible skips. Named client credentials are scoped, and only their hashes are stored. The direct integration API is available for a future Marquee client, but this repository does not provide that client or deploy anything to a TV. Exact range ends and the explicit terminal flag must be respected.

## Detection evidence and limits

Synthetic checks exercise detection and preservation using reproducible generated media. Results are proposals, and ranking scores are not calibrated probabilities.

The detector compares native decoded video and every selected audio channel at fixed offsets, with explicit uncovered gaps and inward range bounds. The picture and the sound of two files get their own offset: the picture proposes one, the sound is looked for within a quarter of a second of it (files of the same show rarely share exact sound/picture sync), and each is then checked strictly at its own. Every main audio channel must match sample-exactly; a difference only in the subwoofer (LFE) channel never vetoes, because that channel carries no speech and is optional in playback, and the evidence lists the channels that were compared. Unknown evidence, keep vetoes and owner approval remain in force. Unmarked color fields are accepted as-is only under matching native declarations: Reprise does not invent a BT.709 profile, convert color, or claim rendered or semantic equivalence. Tiny visual edits can be missed because the comparison tolerates normal encoding noise; quiet or brief story changes can also be missed. Review every proposed skip and add protected ranges for scenes you want to retain. Subtitles and player overlays are not covered; unsupported formats or stream mappings remain inert. Synthetic checks are not a general narrative-safety guarantee.

The one exception to protecting unconfirmed content is Plex's own intro or credits marker for the exact file. When comparing episodes cannot confirm at least half of it, Reprise lists it as a clearly labelled Plex suggestion instead of a protective keep (a smaller confirmed fragment inside it is dropped and noted, not proposed on its own). It skips nothing until you approve that exact range yourself, in a separate and deliberate step for each range, and keep ranges still veto it. Reprise compares at most the three nearest episodes of the season. Because two encodes of one episode rarely match frame for frame, a real intro is often confirmed in fragments; inside a Plex marker, Reprise therefore fills a gap of at most 3 seconds between two pieces it confirmed (and counts it towards the half), unless one of your keep ranges overlaps it. Gaps over 3 seconds, and anything outside the marker, stay protected, so a very short joke inside the intro can be skipped with the rest. A filled gap is shown with its own reason, listed in the result ("Filled 4 short gaps (longest 2.1 s) inside Plex's intro."), and is an ordinary proposal you approve.

Reprise can also ask public intro and credits databases (IntroDB, SkipDB, and TheIntroDB if you gave it a key) where an episode's intro and credits are. It sends only the show's id, season, episode and the file's length. Those times are used in two ways: as hints for where to search in the other episodes (they only add places to look; the strict sound and picture confirmation decides exactly as before), and, when comparing cannot confirm them, as suggestions labelled with the database's name ("IntroDB suggestion", or "Plex + IntroDB suggestion" when Plex says the same). Like a Plex suggestion, one skips nothing until you approve that exact range yourself. The switch "Look up intro times online (IntroDB, SkipDB)" on the Library page turns this off; with it off, or when no database knows the episode, results and speed are the same as before.


## Install and set up

You need three things: a machine that runs Docker (Unraid has it built in), a Plex Media Server your network can reach, and the folder that holds your TV shows. Reprise starts in **review only** mode: it reads your files and asks Plex about your library, and sending results to Plex is switched off.

The route is short:

1. Install Reprise, with the [Unraid template](#install-with-the-unraid-template) or with [Docker Compose](#install-with-docker-compose).
2. Check [every setting](#every-setting-explained) and [share your media folders](#share-your-media-folders-with-reprise).
3. Open Reprise and follow the [Setup guide](#first-start-and-the-setup-guide).

Four words you will meet:

- A **container** is Docker's sealed box for one program. Reprise runs in one, and it only sees the folders you share into it.
- A **host path** is the path of a folder on your server itself, outside any container.
- To **mount** a folder means to share it into the container, at a path you choose. That sharing is also called a mount.
- **Read-only** means Reprise can read the files in a folder but cannot change, move or delete them. That is the right setting for your media.

An unsigned image is published on GitHub Container Registry. The examples pin the accepted image built from commit `6d69bcfd5abeb2b704b0dc1c07f611967446c3f0`; they do not update automatically. There is no versioned release or `latest` tag.

In plain words: Docker downloads Reprise as a ready-made package called an image. This one is not digitally signed. The examples name one exact build by its digest, which is a long fingerprint, so what you install is what was tested. Nothing updates behind your back: to update, you choose a newer digest yourself. [Image delivery and acceptance](#image-delivery-and-acceptance) says which build is current.

Do not expose the interface publicly without appropriate authentication and transport security. In plain words: keep Reprise on your home network. To reach it from outside, put a reverse proxy in front of it. A reverse proxy is a small program that adds a login and HTTPS, which is an encrypted connection. Configure an internal reverse proxy separately; Reprise does not set one up for you.

FFmpeg is the open-source video tool Reprise uses to read your files. Images built from this source use FFmpeg 8.1.2 (GPL, no non-free components) with the matching ffprobe on PATH, NVDEC and VA-API hardware decoding compiled in, and a separate patched CPU-only loudness binary; an image published before GPU decoding was added reports no hardware accelerators in `ffmpeg -hwaccels`. Dolby Vision Profile 5 colour accuracy is not established.

### Install with the Unraid template

Unraid uses templates. A template is a ready-made form that fills in the Docker settings for you. Reprise's template is the file `unraid-templates/reprise.xml` in this repository. It pins the same accepted image digest as Compose.

1. Open a terminal on your Unraid server (the `>_` button at the top right of the Unraid page).
2. Copy the template to the folder where Unraid keeps templates you add yourself:

   ```sh
   mkdir -p /boot/config/plugins/dockerMan/templates-user
   wget -O /boot/config/plugins/dockerMan/templates-user/my-Reprise.xml \
     https://raw.githubusercontent.com/nphil/reprise/main/unraid-templates/reprise.xml
   ```

   No `wget`? Open [the raw file](https://raw.githubusercontent.com/nphil/reprise/main/unraid-templates/reprise.xml) in your browser, copy all of its text, and save it as `my-Reprise.xml` in that same folder.
3. Open the **Docker** tab and click **Add Container**.
4. In the **Template** box at the top, choose **Reprise**. Unraid names each entry after its file, without the leading `my-`, and lists the ones you added yourself under **User templates**.
5. Fill in the fields. Only two need you:
   - **App data**: where Reprise keeps its settings and results. The default, `/mnt/user/appdata/reprise`, is fine.
   - **Media**: the folder that holds your videos. Share the parent folder that holds all your libraries, so one mount covers them. It is shared read-only.

   Leave the rest as it is. **Show more settings** reveals the optional ones; [Every setting explained](#every-setting-explained) says what each does.
6. Click **Apply**. Unraid downloads the pinned Reprise image from GitHub Container Registry, creates the container from your settings and starts it. The first download can take a few minutes. Click **Done** when it finishes.
7. On the Docker tab, click the Reprise icon and choose **WebUI**, or open `http://<your Unraid IP>:8080`. Next: [First start and the Setup guide](#first-start-and-the-setup-guide).

### Install with Docker Compose

Docker Compose starts a container from a short text file. Reprise's example file is `docker-compose.example.yml` in this repository.

1. Make a folder for Reprise and download the example file into it:

   ```sh
   mkdir -p ~/reprise && cd ~/reprise
   curl -L -o docker-compose.yml https://raw.githubusercontent.com/nphil/reprise/main/docker-compose.example.yml
   ```
2. Open `docker-compose.yml` in a text editor and replace the two host paths:
   - `/path/to/reprise/config` becomes the folder where Reprise keeps its settings and results.
   - `/path/to/media` becomes the folder that holds your videos.
3. Start it:

   ```sh
   docker compose up -d
   ```

   The first start downloads the pinned image, so give it a few minutes. If you kept the file's original name, run `docker compose -f docker-compose.example.yml up -d` instead.
4. Open `http://<your server's IP>:8080` in a browser. Next: [First start and the Setup guide](#first-start-and-the-setup-guide).

To change the port, edit the left number in `"8080:8080"`. To change anything later, edit the file and run `docker compose up -d` again: Compose recreates the container with the new settings. A plain `docker restart` does not pick up new folders.

For production, set `IMAGE` to the independently accepted image digest described in [Image delivery and acceptance](#image-delivery-and-acceptance). The table below shows how.

### Every setting explained

Every setting is listed once, with an example for Unraid and one for plain Docker. **Required** settings come first. **Optional** ones can stay empty.

| Setting | What it is | Needed | On Unraid, for example | With Docker, for example |
|---|---|---|---|---|
| **Web UI port** `8080` | The door you open in your browser: `http://<server-ip>:8080`. Reprise listens on TCP 8080 inside its container. | Required | Port `8080`. Change the host port if something else already uses 8080. | `"8080:8080"`. The left number is the port on your server. |
| **App data** `/config`, read-write | Where Reprise keeps its settings, your account, your review decisions and its results. Back this folder up. | Required | `/mnt/user/appdata/reprise` | `/srv/reprise/config:/config:rw` |
| **Media** `/media`, read-only | The folder that holds your videos. Reprise reads files here and can never change them. | Required | `/mnt/user/data` (you choose; the template leaves it empty) | `/srv/data:/media:ro` |
| **Media 2** `/media2`, read-only | A second media folder, for when your videos live in two places. | Optional | `/mnt/user/archive` | `/srv/archive:/media2:ro` (commented out in the example file) |
| **PUID and PGID** | The user and group numbers Reprise runs as. They decide which files it may read and write. | Required, the defaults are fine on Unraid | `99` and `100` | Your own numbers: `id -u` and `id -g` show them (the example file uses Unraid's `99` and `100`) |
| **Working folder** `TMP_FOLDER` | Scratch space for processing. It needs at least 1 GiB free. | Required, the default is fine | `/config/tmp`, which is `/mnt/user/appdata/reprise/tmp` on disk | `TMP_FOLDER: "/config/tmp"` |
| **NVIDIA GPU** `NVIDIA_VISIBLE_DEVICES` and `NVIDIA_DRIVER_CAPABILITIES` | Lets an NVIDIA graphics card decode video. Off by default. | Optional | See [Optional GPU decoding](#optional-gpu-decoding) | See [Optional GPU decoding](#optional-gpu-decoding) |
| **Intel GPU** devices `/dev/dri/...` | Lets an Intel graphics chip decode video. Off by default. | Optional | See [Optional GPU decoding](#optional-gpu-decoding) | See [Optional GPU decoding](#optional-gpu-decoding) |
| **Plex folder** `/plex`, read-write | Plex's whole "Plex Media Server" folder. Only for sending results to Plex. Leave it out for review only. | Optional | `/mnt/user/appdata/plex/Library/Application Support/Plex Media Server` | `"/srv/plex/Library/Application Support/Plex Media Server:/plex:rw"` (commented out in the example file) |
| **`PLEX_CONFIG_FOLDER`** | Tells Reprise where, inside its container, to find that Plex folder: `/plex`. Set it only together with the Plex folder row. Case matters: `/plex` is not `/Plex`. | Optional | `/plex` | `PLEX_CONFIG_FOLDER: "/plex"` (commented out in the example file) |
| **`IMAGE`** | Which image Compose runs. By default it is the pinned image written in the file. | Optional, Compose only | Not used: the template's Repository field holds the image | `IMAGE=ghcr.io/nphil/reprise@sha256:<digest> docker compose up -d`, or put that `IMAGE=` line in a `.env` file next to the compose file |

**The defaults are review only and safe.** Out of the box Reprise reads your media, talks to Plex over the network, and writes only to its own App data folder. Your media is mounted read-only. No Plex folder is mounted, and `PLEX_CONFIG_FOLDER` is empty. The Plex rows above are lines you add on purpose, and only if you decide later to send results to Plex.

A read-only media mount protects your files. It does not control what Reprise may ask a connected Plex to do: other features inherited from Media Preview Generator can write to Plex through its web address (its API), or into Plex's folders, if they are switched on. A writable Plex folder is **not** a default. Mount one only for sending, after backups and checks: see [Turn on sending to Plex](#turn-on-sending-to-plex).

**Keep a review-only install quiet.** Once Plex is connected (step 1 of the [Setup guide](#first-start-and-the-setup-guide)), two Reprise settings make sure nothing writes into Plex's folders. Open Settings, Connections, then **Edit** on your Plex server:

1. On the **Connection** tab, leave **Plex config folder** empty. The setup wizard offers `/plex` there. If you did not share a Plex folder at `/plex`, clear the field.
2. On the **Processing** tab, switch **Write preview thumbnails into Plex** off. Preview thumbnails are the small pictures Plex shows while you move along a video's timeline. Reprise inherited a feature that writes them into Plex's folder, and it starts switched on for a new Plex server.

With both done, the **Setup Health** tab says the Plex folder is not needed, and no preview job writes there. A leftover `/plex` that is not shared stays a critical Setup Health row until you do this. Reprise's older checks expect a Plex config folder to be an existing folder that contains `Media/localhost` whenever a Plex server still has preview thumbnails, chapter thumbnails, Intro & Credits or loudness switched on. With all of those off, a missing folder is not a problem.

**Scratch space.** Reprise needs storage-backed scratch space, a working folder, with at least 1 GiB free. `TMP_FOLDER=/config/tmp` puts it inside App data, which avoids the small `/tmp` memory folder (64 MB) that the examples give the container on purpose. Configuration validation creates the configured folder if needed. Python's own temporary files use the separate `TMPDIR` setting. To redirect those too, create the writable `tmp` folder inside the config volume with the application user's ownership before starting the container, then set `TMPDIR=/config/tmp`. Do not rely on changing this variable after Python has already selected its temporary directory.

### Share your media folders with Reprise

Reprise reads your video files directly, so it has to be able to open them. It can only work on episodes it can open, so this step matters. Here it is slowly.

One word is still new: a **path mapping** is a note that says "the folder Plex calls A is the same folder you gave me as B". You will see when you need one.

Plex tells Reprise where each file is, using Plex's own folder path: for example `/data/tv`. Reprise can only open files through its own mount: for example `/media/tv`. Both paths name the same folder on your disk, but Reprise can only use the second one. A path mapping tells Reprise that `/data/tv` is `/media/tv`.

**A worked example on Unraid**

| | Path |
|---|---|
| Where the folder really is on your server | `/mnt/user/data/tv` |
| What Plex calls it (Plex shares `/mnt/user/data` as `/data`) | `/data/tv` |
| What Reprise calls it (you share `/mnt/user/data` as `/media`, read-only) | `/media/tv` |

Plex says a file is at `/data/tv/Show/S01E01.mkv`. Reprise can only open `/media/tv/Show/S01E01.mkv`. The mapping links them: **Path on the media server** `/data/tv`, **Same folder inside Reprise** `/media/tv`. Add one row for each library folder Plex reports.

You can skip the mapping by sharing the folder at the same path Plex uses. Here that means sharing `/mnt/user/data` as `/data`: on Unraid, change the container path of the **Media** field from `/media` to `/data`. Reprise then finds every file at the path Plex reports, and the mapping table stays empty. Either way, the second path in a mapping has to exist inside Reprise, so it must start with a path you shared.

**Where to set it**

- In the setup wizard, on the step **Where are your files?**: open **Advanced**, then **Media path mapping**, and add a row.
- Later: Settings, Connections, **Edit** on your Plex server, the **Path mappings** tab, **Add row**, then Save. The Setup guide's button for step 2, **Fix media folders**, opens this tab for you.

**How to check one real file**

- On a new install, use **Check a real file** in the wizard step **Where are your files?** Paste the full path of one video file as Plex shows it, then press **Check file**. In Plex's web app, open an episode and choose **Get Info**: the file's path is at the top of that window. Reprise answers **File found and readable.**, or says what is wrong: **File not found. Check the mapping and Docker media mount.** or **File found but not readable. Check the container's permissions.** It also says whether a mapping was applied. To open the wizard again later, add `/setup?rerun=1` to Reprise's address.
- From a console inside Reprise (see [Sign in and your account](#sign-in-and-your-account) for how to open one), list the folder: `ls /media/tv`. If you see your shows, the mount works.
- At any time, step 2 of the Setup guide lists every folder Reprise cannot see and says which mount to add.

**Read-only is correct.** Keep every media mount read-only (`:ro` in Docker, read-only access mode on Unraid). Reprise only reads video files, so it never needs to write to them. If a file is "found but not readable", that is a permissions problem (PUID and PGID), not a reason to make the mount writable.

### First start and the Setup guide

1. Open `http://<your server's IP>:8080` in a browser.
2. The first visit shows **Welcome to Reprise**. Choose a username and a password (at least 8 characters). That is the only account, and you are signed in right away. Do this straight after the first start: until the account exists, anyone who can open the page can create it. If you forget the password, see [I forgot my password](docs/faq.md#i-forgot-my-password).
3. The **Setup guide** opens by itself. It is a checklist of seven steps. Each step says in plain words whether it is done, and has one button that opens the place where you do it. The guide checks your real settings every time you open it, so you can leave and come back.

| # | Step | Needed | What it does |
|---|---|---|---|
| 1 | **Connect Plex** | Required | Sign in with your Plex account and pick your server. This opens the setup wizard and takes about a minute. |
| 2 | **Choose libraries and check your media folders** | Required | Tick the libraries Reprise should read, and make sure Reprise can see the folders they use. See [Share your media folders with Reprise](#share-your-media-folders-with-reprise). |
| 3 | **Refresh the library** | Required | Reprise reads your episode list from Plex. It analyzes nothing and changes nothing in Plex or your files. |
| 4 | **Use a graphics card (GPU)** | Optional | Reprise works on your processor alone. A graphics card only makes video decoding a little faster. See [Optional GPU decoding](#optional-gpu-decoding). |
| 5 | **Look up intro times online** | Optional | On by default. Reprise asks the public databases IntroDB and SkipDB where intros and credits are. It sends only the show's id, season, episode and the file's length, and their answers are only suggestions. |
| 6 | **Connect Sonarr and Radarr** | Optional | Lets Sonarr and Radarr tell Reprise about new files. Skip it if you do not use them. Details below. |
| 7 | **Send results to Plex** | Optional | Off by default. Writes the ranges you approved into Plex. See [Turn on sending to Plex](#turn-on-sending-to-plex). |

When steps 1 to 3 are done, opening Reprise goes straight to the **Library**, where a short tour starts the first time. The guide stays one click away: choose **Setup guide** in the sidebar, or open Settings, Advanced, Account. **Skip for now** on the guide stops it opening by itself. An install that was already set up before the guide existed (it has an account and a connected Plex) does not show the guide by itself, but it is still in the sidebar.

**Step 6 in more detail: Sonarr and Radarr.** A *webhook* is a short message one app sends to another app's web address when something happens. Reprise uses webhooks, not stored Sonarr or Radarr API keys: Sonarr or Radarr sends a message to Reprise, and Reprise checks that it carries your webhook secret.

1. In Reprise, open Settings, Connections, and copy the **Webhook secret**. Step 6 of the Setup guide shows the two addresses: `http://<server-ip>:8080/api/webhooks/sonarr` and `http://<server-ip>:8080/api/webhooks/radarr`. Use `https://` if you have a reverse proxy that offers it, because the secret travels with every message.
2. In Sonarr or Radarr, open Settings, Connect, add a **Webhook**, and paste the address (method POST). Leave **Username** empty and put the secret in **Password**. Press **Test**.
3. If Sonarr or Radarr see your folders at different paths than Plex does, put their path in the **Path used by Sonarr / Radarr** column of the path mapping (Settings, Connections, Edit, Path mappings).

For review only, leave the import and upgrade events off and leave automatic analysis schedules off. A successful **Test** confirms that Reprise can be reached; it does not switch any automation on. These inherited import hooks do not trigger native Reprise analysis (today they only drive Plex preview thumbnails), so see the [thumbnail workflow guide](docs/sonarr-radarr-preview-thumbnails.md) before enabling them. After new episodes arrive, press **Refresh library** in the Library instead.

Known follow-on work is tracked separately: [consistent Reprise navigation across retained pages](https://github.com/nphil/reprise/issues/2) is not claimed as implemented. GPU-assisted analysis ([issue 1](https://github.com/nphil/reprise/issues/1)) is limited to decoding the video that is analysed; see [Optional GPU decoding](#optional-gpu-decoding).

## Sign in and your account

The first visit shows a "Welcome to Reprise" page where you choose a username and a password (at least 8 characters). That is the only account, and the page never appears again once it exists. Until then anyone who can open the page can make it, so do it right after the first start. A short tour of the Library, Analyze and Review follows; Skip closes it for good, and **Take the tour** in the sidebar or under Settings → Advanced → Account opens it again. The same Account section changes the password.

Forgot the password? There is no email reset. Open a console in the container (Unraid: Docker tab, the Reprise icon, **Console**; elsewhere `docker exec -it <container> sh`) and run one of these:

```bash
python3 -m media_preview_generator.web.account_cli reset-password   # a new password for the same account
python3 -m media_preview_generator.web.account_cli reset-account    # start over: the welcome page asks again
```

Both apply at once and sign every browser out. Deleting `auth.json` from the config folder does the same as `reset-account`. Neither touches the webhook secret, your servers or your results.

Sonarr, Radarr, Plex and your own scripts use a separate **webhook secret** (Settings → Connections: show, copy, regenerate). Upgrading from the old token login changes nothing for them: the first start copies the old token (`WEB_AUTH_TOKEN` if it was set, otherwise the one in `auth.json`) into the webhook secret unchanged, and the welcome page then asks for your account. `WEB_AUTH_TOKEN` no longer signs anyone in and can be removed. With `AUTH_METHOD=external` (a proxy or VPN does the signing in) there is no account.

## Optional GPU decoding

A GPU passed into the container decodes the video that analysis compares and the comparison previews. Everything else (audio, the ffprobe audit, the comparisons and every decision) stays on the CPU, so results do not change. With no GPU, or after any GPU failure, the same window decodes on the CPU.

- **What uses the GPU.** H.264 in 8-bit 4:2:0 (Baseline, Main, High) and HEVC in 8-bit or 10-bit 4:2:0 (Main, Main 10), from 144 to 4096 pixels on a side. Everything else (MPEG-2, MPEG-4, VC-1, VP9, AV1, 4:2:2, 4:4:4, H.264 Hi10P) decodes on the CPU.
- **It must match the CPU.** At startup each detected GPU decodes two packaged reference clips and has to match the CPU frame for frame: timestamps, pixel format, colour declarations and every plane checksum. A GPU that differs is not used for that codec and the log says why.
- **It always falls back.** A decode that fails on the GPU is repeated on the CPU, which shows whether the GPU or the file was at fault; a file the CPU cannot decode either fails exactly as it did before. Frames already read are checked against the repeat rather than repeated, so a window is never half one decoder and half another unseen. Five failures in a row pause that GPU for ten minutes.
- **You can see and switch it.** The Library page shows one line under its title, for example `Analysis uses GPU: NVIDIA Tesla P40 (NVDEC decode), then Intel UHD Graphics 630 (VAAPI decode).` or `Analysis uses CPU (no GPU found).`, with a button that turns GPU decoding off and on. The same text is `acceleration.label` in `GET /api/reprise/settings`, and `PATCH /api/reprise/settings` with `{"gpu_decode": false}` does what the button does. A GPU turned off (or given no workers) in the inherited GPU settings is not used here either.
- **What it saves.** Only the video decoder moves. The ffprobe audit decodes the same window again on the CPU and costs about as much as the decoder, so a whole analysis speeds up less than its decode does. Measured on the host above (container limited to one CPU, host busy with other work; 11 episodes from 1080p to 4K, each decoded identically on CPU, NVDEC and VA-API): the P40 cut the decoder's CPU time by 26% (46% for 4K H.264, 10% for 10-bit HEVC) and the windows' wall time by 15%, and one whole analysis finished about 4% sooner. The Intel iGPU did not beat the CPU: 7% more CPU time overall and 12% less at 4K, because every frame is copied back through memory. The P40 is tried first, so the iGPU decodes only what the P40 cannot; to keep the iGPU free for other work, turn that GPU off in the GPU settings.

Settings that work (verified on an Unraid host with a Tesla P40 and an Intel UHD Graphics 630). Preserve the existing resource and security flags and add:

| Setting | Value |
|---|---|
| Extra Parameters | `--runtime=nvidia --group-add=<GID>` (the `GID` is the numeric group of the Intel render node: `ls -ln /dev/dri`; it was 18 here) |
| Variable `NVIDIA_VISIBLE_DEVICES` | the P40's UUID from `nvidia-smi -L` (the template default `void` means no NVIDIA GPU) |
| Variable `NVIDIA_DRIVER_CAPABILITIES` | `compute,video,utility`; `video` supplies the NVDEC library. Never enter a bare `NVIDIA_DRIVER_CAPABILITIES=...` assignment in Extra Parameters: it is not a Docker option |
| Device | `/dev/dri/card1` and `/dev/dri/renderD129`, the Intel nodes on that host. Do not assume `renderD128` is Intel: NVIDIA owns it there |

Do not use privileged mode or change host device permissions. In Docker Compose the same settings are `runtime: nvidia`, the two environment variables, `devices:` for the nodes and `group_add:` (see the commented lines in `docker-compose.example.yml`). Either GPU works alone.

Check it from the host once the container is up:

```sh
docker exec reprise ffmpeg -hide_banner -hwaccels                 # lists cuda and vaapi
docker exec reprise nvidia-smi -L                                 # the NVIDIA GPU
docker logs reprise 2>&1 | grep 'Reprise decodes on the GPU'      # one line per GPU that passed its check
nvidia-smi dmon -s u                                              # on the host: `dec` rises while an analysis runs
```

Intel decoding needs the container's media driver to match its libva; images built from this source carry a matching pair. VA-API on other Intel generations depends on that driver's support for them, and AMD GPUs take the same VA-API path with the image's Mesa driver (not tested here). Whatever the hardware, a GPU is used only after it passes the startup check above. The preview and Intro & Credits workers inherited from the original project keep their own per-GPU worker settings, and they see these GPUs too: a newly detected GPU is enabled there by default with one worker.

## Turn on sending to Plex

**Sending is optional and off by default.** Without it, Reprise only reads: it finds repeated intros and credits, you review them, and nothing in Plex changes. Turn sending on only when you want your approved ranges to appear as skip markers in Plex.

What sending does: Plex keeps its intro and credits markers in its own database, which is a single file. Sending writes your approved ranges straight into that file. A mistake in a database file can damage a whole Plex library, so the rules below are strict. Do them in order.

### Before you start

- **Plex Media Server 1.43.4.x or 1.43.5.x.** Reprise has only been verified with these versions, and it refuses any other ("This Plex version has not been verified for database publication").
- **Plex and Reprise on the same machine.** Reprise opens Plex's database file itself, so the file has to be on a local disk of that machine. A copy of the folder, or a folder reached over the network (NFS or SMB), does not work.
- **Plex running.** Reprise checks that Plex has its database open when you turn sending on, and again for every change.
- **A Plex Pass, to see the result.** Plex serves no intro or credits markers (the Skip buttons) unless the server has a Plex Pass, and the person watching needs a Plex Pass or to be in your Plex Home.
- **One marker made by Plex itself.** Plex creates the one database row that every marker hangs off the first time it finds an intro or credits marker. If your Plex never has, turn on Plex's own intro detection for one library and play an episode first. Afterwards, leave that library's own marker setting on, or Plex hides every skip marker in it.

### The safe steps

1. **Back up Plex's database.** Run this on your server, and change `plex` to the name of your Plex container:

   ```sh
   docker exec plex "/usr/lib/plexmediaserver/Plex SQLite" \
     "/config/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db" \
     ".backup '/config/library-db-before-reprise.db'"
   ```

   It prints nothing when it works. The copy lands in the folder you share with Plex as `/config`; on Unraid that is usually `/mnt/user/appdata/plex/library-db-before-reprise.db`. Check that the file is there with `ls -lh`.

   `Plex SQLite` is the database tool that comes with Plex, and `.backup` makes a consistent copy even while Plex is running. **A plain file copy while Plex runs is not a safe backup**: the copy can mix old and new data and be corrupt (SQLite's own page, [How to corrupt an SQLite database file](https://www.sqlite.org/howtocorrupt.html), says so). If your Plex image keeps its data somewhere else, use the path of `com.plexapp.plugins.library.db` as Plex sees it inside its own container. The file always sits in `Plug-in Support/Databases` inside the `Plex Media Server` folder.
2. **Find Plex's whole "Plex Media Server" folder.** It is the folder that holds `Preferences.xml`, `Plug-in Support` and `Media`. With the usual Plex Docker images it sits inside Plex's `/config` folder, at `Library/Application Support/Plex Media Server`. On Unraid that is usually:

   ```sh
   ls "/mnt/user/appdata/plex/Library/Application Support/Plex Media Server"
   ```

   You should see `Media`, `Plug-in Support` and `Preferences.xml`. Share the whole folder, not a part of it: Reprise reads `Preferences.xml` to know whose database it is, and looks for the database in `Plug-in Support/Databases`.

   **Use the same host path Plex itself uses.** On Unraid, open the Docker tab, click the Plex icon, choose **Edit**, and read the host folder next to `/config`. If Plex says `/mnt/user/appdata/plex`, write `/mnt/user/appdata/plex/...` for Reprise too, not `/mnt/cache/appdata/plex/...`, even though both reach the same disk. SQLite's file locks only work when both programs open the very same file, and on Unraid the user share (`/mnt/user`) and the disk (`/mnt/cache`) can show one file as two different files to those locks. Reprise checks that Plex has the database open through the folder you gave it, and refuses when it does not. Share the real folder: not a copy, and not a network share.
3. **Share it with Reprise, read-write, at `/plex`.** Read-write, because Reprise writes the markers into the database. Reprise runs as the user and group in PUID and PGID, and that user must be allowed to write Plex's files. On Unraid those are usually 99 and 100 for Plex too, the same as Reprise's defaults. Elsewhere, `ls -ln` on the folder shows the owner's numbers, and PUID and PGID must match them.
   - **Unraid:** Docker tab, Reprise icon, **Edit**, **Show more settings**. Set **Plex folder (optional, for sending)** to the host path from step 2 (its container path `/plex` and read-write mode are already set). Set **Plex folder variable (optional, for sending)** to `/plex`. Click **Apply**.
   - **Docker Compose:** in `docker-compose.yml`, remove the `# ` in front of the two lines marked `(optional)` for Plex (the `/plex` volume and `PLEX_CONFIG_FOLDER`), put your host path in the volume line, then run `docker compose up -d`.

   Recreate only Reprise. Never restart Plex: it keeps running and needs no change. Unraid's **Apply** and `docker compose up -d` both recreate the container with the new folder, while a plain restart does not add a new folder. Linux paths are case-sensitive, so `/plex` and `/Plex` are different folders.
4. **Tell Reprise where the folder is.** Open Settings, Connections, then **Edit** on your Plex server. On the **Connection** tab, set **Plex config folder** to `/plex` and Save. Reprise copies `PLEX_CONFIG_FOLDER` into its settings on the very first start only. On an install that has already run, this field is the one that counts.
5. **Turn sending on.** Open Settings, **Sharing**, and switch on **Allow sending to Plex**. Reprise asks you to confirm; choose **Allow**. It then checks your setup with up to 25 episodes from your library, and one that passes every check is enough. If something is missing, the switch stays off and the reason appears under it (see [Troubleshooting](#troubleshooting)).
6. **Send one change, and confirm each time.** Open an episode in the Library, review it and save your review (saving never changes Plex). In **Send to Plex**, choose the Plex copy, then press **Prepare a Plex change** and read the current and proposed markers. Nothing is written yet. Press **Apply to Plex** and confirm. Reprise writes only if Plex still matches what you reviewed; otherwise it refuses rather than guesses. It touches only intro and credits markers.

### How to undo

- **Stop sending.** Switch **Allow sending to Plex** off. While it is off, Reprise cannot apply any change to Plex.
- **Undo one change.** Open the episode and its **Send to Plex** section. It lists every change Reprise prepared or applied for that episode. Press **Undo** on an applied one and confirm. Undo needs the switch on, so switch sending off only after you have undone what you want to undo. Undo refuses when Plex changed those rows since, for example when Plex found its own markers meanwhile, so it never overwrites someone else's change.
- **Go back to your backup.** If something goes badly wrong, stop Plex and put your backup in place of its database. Plex's page [Restore a database backed up via Scheduled Tasks](https://support.plex.tv/articles/202485658-restore-a-database-backed-up-via-scheduled-tasks/) lists the steps: move the old database and its `-shm` and `-wal` files aside, copy the backup in under the original name, make sure Plex can read and write it, then start Plex.

## Troubleshooting

Find the words you see in the first column. Where a row says **Edit**, it means Settings, Connections, then **Edit** on your Plex server.

| What you see | Why | What to do |
|---|---|---|
| **Sending to Plex needs Plex's database, and Reprise can't see it: ...** (under the Sharing switch; step 7 of the Setup guide names the same reason) | Reprise looks for Plex's database file inside its own container and cannot use it. The words after the colon say which case: the Plex config folder is not set; nothing is shared at that folder; the folder is there but Plex's database is not in it (only part of Plex's folder is shared, for example just `Media/localhost`); Plex's `Preferences.xml` is missing or belongs to a different Plex; Reprise is not allowed to write the database; or the database is on a network share. | Share Plex's whole "Plex Media Server" folder, read-write, at the path in **Edit**, **Connection** tab, **Plex config folder**. Follow [Turn on sending to Plex](#turn-on-sending-to-plex). |
| **Sending to Plex can't be turned on: ...** | The folder is fine, but one of Reprise's safety checks failed for every episode it tried (up to 25). It shows the most common reason. | Look the reason up in the second table below. |
| **Plex database not found at ...** (Setup Health, in the Intro & Credits part, when that feature is on) | The Plex config folder is set, but `Plug-in Support/Databases/com.plexapp.plugins.library.db` is not inside it. | Share the whole "Plex Media Server" folder. `ls` on it should show `Media`, `Plug-in Support` and `Preferences.xml`. |
| **Plex's database is on a network share ...** (same places) | The database is on a network drive such as NFS or SMB, or on a disk type Reprise cannot tell is local. SQLite cannot share its file locks over a network, so Reprise refuses. | Run Reprise on the same machine as Plex and share the real, local Plex folder. |
| **This app (user ...) can't write ...** (same places) | Reprise is not allowed to write Plex's database or folder. Either the folder is shared read-only (`:ro`), or the files belong to a different user than the one Reprise runs as. | Share the folder read-write. Set PUID and PGID to the owner of Plex's files (`ls -ln` shows the numbers). |
| **output.plex_config_folder '...' does not exist on this container** (a red message when you press Save in **Edit**), or **'...' does not exist on this container. Verify the mount is correct.** (Setup Health) | A Plex config folder is typed in (the setup wizard offers `/plex`), but nothing is shared into Reprise at that path. | Review only: clear **Plex config folder** on the **Connection** tab, switch **Write preview thumbnails into Plex** off on the **Processing** tab, and save. Sending: share the Plex folder at that path first, then save. |
| **Media folder unavailable**, **File unavailable** or **File missing** in the Library, or **Reprise can't see ...** in step 2 of the Setup guide | Reprise cannot open a folder or file Plex told it about. The folder is not shared at the path Reprise looks at, a path mapping is missing or wrong, your storage was not ready when Reprise started, or the file was renamed or replaced (for example by a Sonarr or Radarr upgrade). | Follow [Share your media folders with Reprise](#share-your-media-folders-with-reprise), then press **Refresh library**. A folder that is there but empty may be a network share that was not ready: restart Reprise once the share is up. If Reprise cannot read it, check the share's permissions for the user Reprise runs as (PUID and PGID). |
| **No Plex library is connected yet.** (Library) | Plex is not connected, or no library is chosen. | Do steps 1 and 2 of the Setup guide, or open Settings, Connections. |
| **Connection failed** (setup wizard, or a server card in Connections) | Reprise cannot reach Plex or sign in: the address is wrong, Plex is not running, or the sign-in is no longer valid. | Use your server's IP and Plex's port, for example `http://192.168.1.100:32400`, not `localhost`. Inside Reprise's container, `localhost` means Reprise itself. Check that Plex is running, and sign in again if asked. |
| **401** or **Authentication required** when Sonarr or Radarr tests the webhook | The webhook secret is missing or wrong. | In the Sonarr or Radarr webhook, leave **Username** empty and put the webhook secret (Settings, Connections, Webhook secret) in **Password**. If you regenerated the secret, update every sender. If they cannot reach Reprise at all, use the server's IP, not `localhost`, and port 8080. |
| **Publication conflict; refresh and try again** (**Prepare a Plex change** or **Apply to Plex**) | Something changed since you reviewed the episode (the file, Plex's details for it, or your saved review), or a safety check refused. Reprise refuses rather than guesses. | Refresh the library, open the episode again, save your review, and prepare the change again. If every episode is refused, go through "Before you start" in [Turn on sending to Plex](#turn-on-sending-to-plex), including Plex's own marker. |

After **Sending to Plex can't be turned on:** the reason that follows is one of these:

| Reason shown | What it means | What to do |
|---|---|---|
| The local Plex database did not pass its safety checks | Reprise found the file, but a check failed. The checks are: the database is on a local disk, Reprise can write it, and Plex has it open through the very same folder. Plex may be stopped, Reprise may reach the file by a different path than Plex does, the folder may be shared read-only, or the database may be on a network share. | Start Plex. Use exactly the host path Plex uses. Share read-write. See the rows above. |
| This Plex version has not been verified for database publication | Reprise only sends to Plex Media Server 1.43.4.x or 1.43.5.x. | Use one of those versions. |
| The local database belongs to a different Plex server | `Preferences.xml` in the shared folder names a different Plex than the one Reprise is connected to. | Share the folder of the Plex you connected. |
| The connected Plex server identity changed | The Plex Reprise is connected to now is not the Plex the episode was read from. | Check the address in **Edit**, then refresh the library. |
| The publication target is not current and uniquely bound, or no episodes are in the library yet | The episode changed since Reprise read it, or there is no episode to check. | Refresh the library and try again. |

## Image delivery and acceptance

The image workflow makes one cold candidate build, pushes it by digest, and checks the bundled source and license artifacts, FFmpeg configuration, and startup health before publishing a full source-commit tag. A tag identifies the source revision; use the immutable image digest to ensure the image deployed is exactly the one independently tested and accepted. These automated checks do not establish legal compliance or replace independent acceptance.

The current image, built from source `6d69bcfd5abeb2b704b0dc1c07f611967446c3f0`, is `ghcr.io/nphil/reprise@sha256:484b02fb7d2f12119384ff660a3d266d754fb88fb79a629c8d50278081947935`. It passed the candidate image checks and [full CI](https://github.com/nphil/reprise/actions/runs/41728941081). After deployment on the reference Unraid host it started healthy, migrated the old sign-in token unchanged to the webhook secret, opened first-run account setup, and its startup checks matched NVIDIA Tesla P40 (NVDEC) and Intel UHD 630 (VAAPI) decoding frame for frame against the CPU on the reference clips. Both deployment examples pin this exact image; use the digest rather than rebuilding or relying on a tag alone.

The earlier detector candidate from source `170b633b8e6adeefadf5201106fbfce51a19e8e5` passed the independent native-engine holdout. This image changes the verification profile (audio timestamp tolerance, 3 nearest references, Plex suggestions), and that holdout has not been repeated for it.

An earlier two-cold-build comparison of revision `ac40` found matching runtime configuration and uncompressed layer inventories; source, license and startup checks passed. That comparison concerned a different revision and does not establish that current rebuilds are identical.

## Publication safety

Plex publication is off by default. Preparation may be reviewed without enabling writes; applying or undoing a change requires explicit enablement and current, approved, compatible results for every playable version. Unsupported, missing or mismatched versions and unsafe end-of-file ranges are refused rather than guessed. Before a Plex write, Reprise records intent separately; after interruption it compares the recorded before and after states and marks any other state uncertain rather than replaying blindly. Undo refuses when the affected Plex state has changed since publication. Back up Plex and validate on disposable data before considering production use.

Saving or locking review decisions does not publish them. Prepare, inspect and apply are separate steps; incompatible disjoint or protected ranges are refused rather than merged into an unsafe Plex marker. Recovery does not blindly repeat an interrupted write. The retained upstream guides describe Media Preview Generator and can differ from this fork; use this README and the [Reprise API guide](docs/reprise-api.md) for current behavior.

## Attribution and provenance

Reprise derives from [Media Preview Generator](https://github.com/stevezau/media_preview_generator), MIT copyright 2025 Steve Adams and contributors; see [LICENSE](LICENSE). The repository preserves its history from commit `49fd9f6592d7b74139d31075a75f83bdf1a3bb28`. [Recurring Content Detector](https://github.com/nielstenboom/recurring-content-detector), MIT copyright 2019 Niels ten Boom, is retained with its [license](third_party/recurring-content-detector/LICENSE) and non-squashed history. The original revision was `b248e17fa9f8d384ae8a146abc7a5d47190aaaa6`; the filtered imported revision is `9905c17c9e17a08a1b99bd90708d867f978015eb`. Two film-derived test videos with unverified redistribution rights were removed from every imported revision, so their original integration tests need independently licensed fixtures.

The bundled Manrope font identifies itself as version 4.504, copyright 2019 The Manrope Project Authors. Its [SIL Open Font License 1.1 and copyright notice](media_preview_generator/web/static/fonts/MANROPE-LICENSE.txt) are distributed alongside the font; the [Manrope project](https://github.com/sharanda/manrope) is its upstream.

The FFmpeg built from the current source includes software based in part on the work of the Independent JPEG Group. Its source, build configuration and applicable license notices are included in newly built images.

To update the detector, make a disposable clone and filter it *before* pulling it into this repository. Replace `<revision>` with the chosen upstream commit. Do not fetch its unfiltered history into Reprise.

```sh
git clone --no-tags https://github.com/nielstenboom/recurring-content-detector.git /tmp/reprise-rcd-update
git -C /tmp/reprise-rcd-update checkout -B filtered <revision>
git -C /tmp/reprise-rcd-update filter-branch --force --index-filter 'git rm --cached --ignore-unmatch tests/data/video0.mp4 tests/data/video1.mp4' --prune-empty -- filtered
git subtree pull --prefix=third_party/recurring-content-detector /tmp/reprise-rcd-update filtered

Review new upstream assets and licensing before an update; filtering those two known paths alone does not prove other material is redistributable. Record the original and filtered revision with each update.

The GPU capability probe clip is generated synthetic video, not footage. Reproduce it with FFmpeg:

```sh
ffmpeg -f lavfi -i testsrc2=size=320x240:rate=24:duration=2 -an -c:v libx264 -pix_fmt yuv420p -movflags +faststart -y media_preview_generator/gpu/assets/test_video.mp4
```
