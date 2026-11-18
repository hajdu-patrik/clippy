# Connecting accounts and posting clips

Clippy can post a finished clip straight to your own channels: the **Post** button on a clip card, or the chat
(*"posztold ki a 2. klipet YouTube-ra és TikTokra"*, *"upload clip 3 to Instagram"*). The video goes up with its post
text: the teaser, the comment question, the link of the full video, your profile links and the hashtags; YouTube also
gets the clip's title.

Each platform needs a free developer app of your own, made once. Clippy never sees your passwords: you sign in on the
platform's own page, and the tokens it gives back stay on this computer in `data/web/accounts.json` (ignored by git,
never sent to the browser). **Disconnect** in Settings deletes them; you can also revoke Clippy's access on the
platform's account page at any time.

Accounts are connected on the computer that runs Clippy (Settings, Accounts). Paired phones can post, but cannot
connect accounts.

## What each platform allows

*Settings, Accounts* shows one card per platform. Clippy only uses each platform's public, official API and its
official sign-in; where a platform offers apps no way to upload a video, the card says so instead of faking it.

| Platform | Connect | Post | Plan a post | What the platform requires |
| --- | --- | --- | --- | --- |
| YouTube | yes (Google, PKCE) | yes, Shorts up to 3 min | yes | a Google Cloud project; uploads of an unaudited project may stay private |
| Instagram | yes (token you generate) | yes, Reels up to 15 min | yes | a Meta app with Instagram login, Creator or Business account |
| Facebook | yes (Facebook Login) | yes, Page Reels 3 to 90 s | yes | a Meta app and a Facebook Page you manage |
| TikTok | yes (PKCE) | drafts, or private until its audit | yes | a TikTok developer app, Sandbox or audit |
| X | yes (OAuth 2.0, PKCE) | yes | yes | an X developer app with credits (pay per use) |
| Twitch | yes (shows the channel) | **no**: no upload API | - | a Twitch developer app |
| Kick | yes (PKCE, shows the channel) | **no**: no upload API | - | a Kick developer app |

"Plan a post" is Clippy's own scheduler (see below): Clippy has to be running at the planned time. The platforms' own
scheduling is not used, so every platform plans the same way.

The official documentation this is built on (checked November 2026):
[YouTube resumable upload](https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol),
[Google OAuth for desktop apps](https://developers.google.com/identity/protocols/oauth2/native-app),
[Instagram content publishing](https://developers.facebook.com/docs/instagram-platform/content-publishing/),
[Facebook Reels publishing](https://developers.facebook.com/docs/video-api/guides/reels-publishing),
[Facebook Login, manual flow](https://developers.facebook.com/docs/facebook-login/guides/advanced/manual-flow),
[TikTok Content Posting API](https://developers.tiktok.com/doc/content-posting-api-get-started),
[X chunked media upload](https://docs.x.com/x-api/media/quickstart/media-upload-chunked),
[X OAuth 2.0 with PKCE](https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code),
[Twitch API reference](https://dev.twitch.tv/docs/api/reference/),
[Twitch authorization code flow](https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/),
[Kick OAuth 2.1](https://docs.kick.com/getting-started/generating-tokens-oauth2-flow.md) and
[Kick scopes](https://docs.kick.com/getting-started/scopes.md).

Every app's redirect URI has the same shape (with your `CLIPPY_PORT` if you changed it); `<name>` is `youtube`,
`facebook`, `tiktok`, `x`, `twitch` or `kick`:

```text
http://127.0.0.1:7860/oauth/<name>/callback
```

## YouTube

1. Open the [Google Cloud Console](https://console.cloud.google.com/), create a project, then under
   **APIs & Services, Library** enable **YouTube Data API v3** and **YouTube Analytics API** (the second one lets
   Clippy read how much of each clip people watch).
2. **Google Auth Platform** (or **OAuth consent screen**): audience **External**. On the **Branding** page:

   | Field | Value |
   | --- | --- |
   | App name, User support email, Developer contact | a name of your choice and your own email |
   | App logo | leave it empty: a logo makes a published app need Google's verification |
   | Application home page | `https://clippyai.vercel.app/` |
   | Application privacy policy link | `https://clippyai.vercel.app/privacy.html` |
   | Authorized domains | `clippyai.vercel.app` only |

   Google asks for the home page and the privacy policy before it lets the app out of *Testing*. Under **Data
   access** add the scopes `youtube.upload`, `youtube.readonly` and `yt-analytics.readonly`.
3. Under **Audience** press **Publish app** and confirm; do not start the verification it offers. While the app is in
   *Testing*, Google ends the sign-in after 7 days and you would have to connect again every week. A published app
   that is not verified shows a warning page at sign-in; that is expected for an app only you use: choose *Advanced*
   and continue.
4. **Clients, Create client**: application type **Desktop app**. Put its id and secret in `.env`:

   ```ini
   CLIPPY_YOUTUBE_CLIENT_ID=...apps.googleusercontent.com
   CLIPPY_YOUTUBE_CLIENT_SECRET=...
   ```

5. Restart Clippy (`clippy restart`), open Settings and press **Connect** next to YouTube.

Good to know:

* Google may keep uploads of a new, unaudited API project **private** until the project passes the YouTube API
  compliance audit. The clip card then says so; you can make the video public in YouTube Studio.
* The default API quota allows only a few uploads a day. A Short can be at most 3 minutes long.

## Instagram

Works with an Instagram **Creator** or **Business** account; no Facebook Page is needed.

1. On [Meta for Developers](https://developers.facebook.com/apps/) create an app with the use case for managing
   content on Instagram (*Instagram API with Instagram login*).
2. Under **App roles, Roles** add your Instagram account as an **Instagram Tester**, then accept the invite in the
   Instagram app (Settings, Website permissions, Apps and websites, Tester invites).
3. In **API setup with Instagram login** add your account and press **Generate token**. Allow the permissions it
   asks for (`instagram_business_basic`, `instagram_business_content_publish`, and
   `instagram_business_manage_insights` for the numbers) and copy the token.
4. In Clippy: Settings, Accounts, **Paste token** next to Instagram, then **Connect**.

The token lasts 60 days; Clippy renews it by itself well before that, as long as it posts or checks the account now
and then. If it has run out, generate a new token and paste it again.

Good to know: Reels can be 3 seconds to 15 minutes long, captions at most 2200 characters with up to 30 hashtags, and
Instagram limits how many posts an account can publish through the API in 24 hours.

## TikTok

1. On [TikTok for Developers](https://developers.tiktok.com/) create an app. Under **Basic information**:

   | Field | Value |
   | --- | --- |
   | App icon | a 1024 × 1024 px PNG or JPG |
   | Category, Description | for example *Photo & Video* and *Cuts my long videos into short clips and posts them to my TikTok drafts.* |
   | Terms of Service URL | `https://clippyai.vercel.app/terms.html` |
   | Privacy Policy URL | `https://clippyai.vercel.app/privacy.html` |
   | Platforms, Web/Desktop URL | **Desktop**, `https://clippyai.vercel.app/` (the redirect URI goes into Login Kit, not here) |

   Without a review, use the app's **Sandbox** (the switch at the top of the app page): create one, do steps 2-4 there,
   add your TikTok account under **Target users**, and take the keys from the Sandbox's **Credentials**.
2. Add the products **Login Kit** and **Content Posting API**. Turn on **Direct Post** in the Content Posting API if you
   want the "posted, visible only to me" mode.
3. In Login Kit add the redirect URI (with your `CLIPPY_PORT` if you changed it):

   ```text
   http://127.0.0.1:7860/oauth/tiktok/callback
   ```

4. The scopes Clippy asks for are `user.info.basic`, `video.upload` and `video.publish`. If your app has no Direct Post,
   set `CLIPPY_TIKTOK_SCOPES=user.info.basic,video.upload` in `.env`.
5. Put the app's keys in `.env`, restart Clippy and press **Connect** next to TikTok:

   ```ini
   CLIPPY_TIKTOK_CLIENT_KEY=...
   CLIPPY_TIKTOK_CLIENT_SECRET=...
   ```

TikTok allows two modes:

| Mode | What happens |
| --- | --- |
| **To drafts** (default) | The video arrives in the TikTok app as a notification; you finish and post it there (sound, cover, text). |
| **Posted, visible only to me** | Posted at once as private. Until TikTok reviews your app, this is the only Direct Post it allows. |

The Sandbox works for its target users before TikTok's review; making posts public straight from Clippy needs the
review (TikTok calls it an audit).

## Facebook

Posts Reels to a Facebook **Page** (the API cannot post a Reel to a personal profile). You need a role on the Page that
can create content.

1. On [Meta for Developers](https://developers.facebook.com/apps/) create an app (the one for Instagram can be reused)
   and add the **Facebook Login** product (or **Facebook Login for Business**, see step 5).
2. In Facebook Login, settings, add `http://127.0.0.1:7860/oauth/facebook/callback` to **Valid OAuth Redirect URIs**.
3. Under **Settings, Basic** copy the app id and secret into `.env`:

   ```ini
   CLIPPY_FACEBOOK_APP_ID=...
   CLIPPY_FACEBOOK_APP_SECRET=...
   ```

4. Restart Clippy and press **Connect** next to Facebook. Clippy asks for `pages_show_list`, `pages_read_engagement`
   and `pages_manage_posts`, finds the Pages you manage and keeps the first one's access token (a Page token made from
   a long-lived sign-in does not expire). To pick another Page set `CLIPPY_FACEBOOK_PAGE_ID=<the Page's id>`.
5. An app that uses **Facebook Login for Business** names a saved configuration instead of the permissions: create one
   with the three permissions and set `CLIPPY_FACEBOOK_CONFIG_ID=<its id>`.

Good to know:

* In *Development* mode the app works for people who have a role on it (admin, developer, tester). Meta's **App
  Review** (and Business Verification) is for apps other people use; an app only you use for your own Page stays in
  development mode.
* Reels must be 9:16, 3 to 90 seconds, 24 to 60 fps, at least 540 × 960 px. Meta allows 30 API-published posts per Page
  in 24 hours. Page Reels are public.
* The Reel is posted at once; the card links `facebook.com/reel/<id>`.

## X

1. In the [X Developer Console](https://developer.x.com/) create a project and an app. Under **User authentication
   settings** choose **Native App** (a public client without a secret) or **Web App** (confidential), set the app
   permissions to **Read and write** and the callback URI to `http://127.0.0.1:7860/oauth/x/callback`.
2. Put the client id in `.env`; only a confidential app also has a client secret:

   ```ini
   CLIPPY_X_CLIENT_ID=...
   CLIPPY_X_CLIENT_SECRET=...   # Web App only
   ```

3. Restart Clippy and press **Connect** next to X. Clippy asks for `tweet.read`, `tweet.write`, `users.read`,
   `media.write` and `offline.access` (so it stays signed in: access tokens last 2 hours and are renewed by themselves).

Good to know:

* X's API is billed **per use** (credits on your developer account): a post costs a small fixed amount, more when its
  text contains a link, and without credits X refuses the call (the card shows X's reason).
* The post text is cut to 280 characters (whole paragraphs, the first one cut at a word); X Premium's longer posts are
  not used. An ordinary account can post videos up to 20 minutes.
* The video goes up in 4 MB chunks (media upload v2: initialize, append, finalize; Clippy waits until X has processed
  it) and is attached to the post. X allows 100 posts per 15 minutes per user.

## Twitch and Kick: connect only

Neither platform lets an app upload a video. Twitch's API has no upload endpoint (it can create clips from a live
stream and read or delete videos) and Kick's public API has none either. The only ways left are private endpoints or
automating the website, which Clippy does not do. So these two cards connect the account (to show whose channel it is)
and say "Connect only"; the Post dialog and the chat never offer them. Clips are still made *from* Twitch and Kick
streams without any account (links, the latest stream by name, daily picks).

**Twitch.** On the [Twitch developer console](https://dev.twitch.tv/console/apps) register an application (your
account needs two-factor authentication) with client type **Confidential** and the OAuth redirect URL
`http://127.0.0.1:7860/oauth/twitch/callback`. Click **New Secret** and put both values in `.env`:

```ini
CLIPPY_TWITCH_CLIENT_ID=...
CLIPPY_TWITCH_CLIENT_SECRET=...
```

Twitch's authorization code flow has no PKCE, so it needs the secret, which Clippy keeps only in `.env`. Clippy asks
for the one scope `user:read:email` (change it with `CLIPPY_TWITCH_SCOPES`); it never reads the email and only calls
*Get Users* to show your display name.

**Kick.** In your Kick account settings (two-factor authentication on) open the **Developer** tab and create an app
with the redirect URL `http://127.0.0.1:7860/oauth/kick/callback`. Put its client id and secret in `.env`:

```ini
CLIPPY_KICK_CLIENT_ID=...
CLIPPY_KICK_CLIENT_SECRET=...
```

Kick uses OAuth 2.1 with PKCE; Clippy asks for `user:read` and `channel:read`.

## Planned and automatic posts

- **At a set time:** in the Post dialog choose *At a set time* and a date. The card lists the planned post with a
  *cancel* link; at its time it starts like a post made by hand. A clip deleted in the meantime is not posted.
- **Automatically for a watched channel:** under *Settings, Automation* give the channel the platforms and the
  daily times (for example *18:00, 21:00*). Each new clip of that channel takes the next free time, one clip per time
  across all channels, best clip first. YouTube posts go public, TikTok to your drafts.

Clippy has to be running at the planned time; a post whose time passed while it was off goes out when it starts again.

## Learning from the results

Every few hours Clippy reads back how the clips it posted are doing: views, likes, comments, shares and, where the
platform tells, how much of the clip people watch (YouTube Analytics, Instagram's average watch time). The numbers
show under each post on the clip card. TikTok drafts have no public video yet, and Facebook and X posts are not read
back yet, so only YouTube and Instagram posts are measured.

*Settings, Results* compares the clips with your own usual (the median views on the same platform), from 5 clips
that are at least two days old: which openings, lengths, kinds of moment and posting times do clearly better or
worse. The clear patterns go to the clip picker as a soft preference (a clearly better moment still wins), and the
chat can answer *"mi működik a legjobban?"* with your own numbers. An account connected to YouTube before this
needs to be connected again once for the watch share.

## When something goes wrong

The clip card shows each platform's result: a link to the post, or what went wrong and what to do. Posting one
platform never stops the others, and a platform that already has the clip is not posted to again when the job is
retried or Clippy restarts.

| Message | What to do |
| --- | --- |
| not set up yet | the developer app's keys are missing from `.env`; add them and restart Clippy |
| no account is connected | Settings, Accounts, Connect |
| the sign-in has run out | connect the account again |
| allows this only after its app review | use the private or draft mode until the platform reviews your app |
| refused the post | the platform's own reason follows (for example the daily limit or the clip's length) |
| too many requests | the platform's rate limit; try again later |
| no Page was found | Facebook: sign in with a person who manages a Page, or check `CLIPPY_FACEBOOK_PAGE_ID` |
| does not let apps upload videos | Twitch and Kick: there is no upload API, so Clippy cannot post there |
