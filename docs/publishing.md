# Posting clips to YouTube, Instagram and TikTok

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

## YouTube

1. Open the [Google Cloud Console](https://console.cloud.google.com/), create a project, then under
   **APIs & Services, Library** enable **YouTube Data API v3**.
2. **Google Auth Platform** (or **OAuth consent screen**): audience **External**, fill in the app name and your
   email. Under **Data access** add the scopes `youtube.upload`, `youtube.readonly` and `yt-analytics.readonly`
   (the last one lets Clippy read how much of each clip people watch; enable the **YouTube Analytics API** too).
3. Under **Audience** press **Publish app**. While the app is in *Testing*, Google ends the sign-in after 7 days and you
   would have to connect again every week. A published app that is not verified shows a warning page at sign-in;
   that is expected for an app only you use: choose *Advanced* and continue.
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

1. On [TikTok for Developers](https://developers.tiktok.com/) create an app and choose **Desktop** as its platform.
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

Your app works for your own account before TikTok's review; making posts public straight from Clippy needs the
review (TikTok calls it an audit).

## Planned and automatic posts

- **At a set time:** in the Post dialog choose *At a set time* and a date. The card lists the planned post with a
  *cancel* link; at its time it starts like a post made by hand. A clip deleted in the meantime is not posted.
- **Automatically for a watched channel:** under *Settings, Watched channels* give the channel the platforms and the
  daily times (for example *18:00, 21:00*). Each new clip of that channel takes the next free time, one clip per time
  across all channels, best clip first. YouTube posts go public, TikTok to your drafts.

Clippy has to be running at the planned time; a post whose time passed while it was off goes out when it starts again.

## Learning from the results

Every few hours Clippy reads back how the clips it posted are doing: views, likes, comments, shares and, where the
platform tells, how much of the clip people watch (YouTube Analytics, Instagram's average watch time). The numbers
show under each post on the clip card. TikTok drafts have no public video yet, so TikTok posts are not measured.

*Settings, What works* compares the clips with your own usual (the median views on the same platform), from 5 clips
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
