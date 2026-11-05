# Reprise Unraid template

`reprise.xml` is a user template for Unraid's Docker tab. A template is a ready-made form: it fills in Reprise's Docker settings, so you only choose your folders.

Review only is the default. Your media is shared read-only and no Plex folder is shared. The Plex fields below are optional, and they stay empty until you fill them in on purpose.

## Install

1. Copy the template to the folder where Unraid keeps templates you add yourself. In an Unraid terminal run:

   ```sh
   mkdir -p /boot/config/plugins/dockerMan/templates-user
   wget -O /boot/config/plugins/dockerMan/templates-user/my-Reprise.xml \
     https://raw.githubusercontent.com/nphil/reprise/main/unraid-templates/reprise.xml
   ```

   No `wget`? Open `reprise.xml` on GitHub, copy all of its text and save it as `my-Reprise.xml` in that same folder. Unraid shows each template under its file's name without the leading `my-`, so this one appears as **Reprise**.
2. Open the **Docker** tab, click **Add Container**, and choose **Reprise** in the **Template** box (under **User templates**).
3. Set **Media** to the folder that holds your videos. Leave everything else as it is.
4. Click **Apply**. Unraid downloads the pinned image from GitHub Container Registry, creates the container with your settings and starts it.
5. Click the Reprise icon and choose **WebUI**, or open `http://<your Unraid IP>:8080`. On the first start, create your account, then follow the Setup guide.

The main README has the full walkthrough: [Install and set up](../README.md#install-and-set-up), [Every setting explained](../README.md#every-setting-explained) and [Share your media folders with Reprise](../README.md#share-your-media-folders-with-reprise).

## Fields

| Field | What it sets | Required | Default |
|---|---|---|---|
| Web UI | port `8080` | Required | `8080` |
| App data | path `/config`, read-write | Required | `/mnt/user/appdata/reprise` |
| Media | path `/media`, read-only | Required | empty: you choose |
| Media 2 (optional) | path `/media2`, read-only | Optional | empty |
| Plex folder (optional, for sending) | path `/plex`, read-write | Optional | empty |
| Plex folder variable (optional, for sending) | variable `PLEX_CONFIG_FOLDER` | Optional | empty |
| PUID and PGID | variables: the user and group Reprise runs as | Required | `99` and `100`, Unraid's own |
| Working folder | variable `TMP_FOLDER`: scratch space with at least 1 GiB free | Required | `/config/tmp` |
| NVIDIA GPU UUID and NVIDIA driver capabilities | variables for GPU decoding | Optional | `void` and `compute,video,utility` |

Everything except Web UI, App data and Media is under **Show more settings**.

## Empty optional fields do nothing

Unraid ignores an empty optional Path. With no host folder entered, it creates no mount, so the container sees nothing at that path. That is why **Media 2** and **Plex folder** can ship in the template and a fresh install is still review only. An empty variable such as the Plex folder variable reaches Reprise as an empty value, which Reprise treats as not set.

Unraid fills an empty value with the field's Default, so these optional paths have an empty Default as well. Keep it that way if you edit the template.

Fill in **Plex folder** and **Plex folder variable** together, and only to send results to Plex. Share Plex's whole "Plex Media Server" folder, from the same host path Plex itself uses, and back up Plex's database first. The steps are in [Turn on sending to Plex](../README.md#turn-on-sending-to-plex).

## Image and GPU

The template pins the same unsigned image as the main README and the Compose example. [Image delivery and acceptance](../README.md#image-delivery-and-acceptance) names its source commit and digest. To update, change the Repository field to a newer published image digest.

GPU decoding is optional and off by default (`NVIDIA_VISIBLE_DEVICES` is `void`, no device is mapped). To use a GPU, set the NVIDIA UUID, add `--runtime=nvidia --group-add=<GID>` to Extra Parameters and add the Intel `/dev/dri` nodes as Device fields; the exact settings, the checks and the fallback behavior are in the README's [Optional GPU decoding](../README.md#optional-gpu-decoding). An image published before GPU decoding was added ignores these settings and decodes on the CPU.
