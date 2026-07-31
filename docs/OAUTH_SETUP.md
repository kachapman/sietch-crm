# OAuth 2.0 Provider Setup

You need to register an app with each provider to get `client_id` and `client_secret` for the IMAP OAuth flow.

---

## Microsoft 365 / Entra

> **Important for personal (Outlook.com / live.com) accounts:** the app's **Supported account types** MUST include personal Microsoft accounts, and the CRM now always uses the `common` authority (the `OAUTH_MICROSOFT_TENANT` value is ignored for the Microsoft provider — it exists for backward compatibility). A tenant-scoped registration mints host-tenant B2B-guest tokens (`#EXT#` UPN) that Exchange Online rejects for IMAP/SMTP XOAUTH2.

### Registering a new app

1. Go to https://entra.microsoft.com → **App registrations** → **New registration**
2. Fill in:
   - **Name**: `Sietch CRM` (or anything)
   - **Supported account types**: "Accounts in any organizational directory and personal Microsoft accounts" (this is the setting that lets personal Outlook.com/live.com accounts authenticate — required for this mailbox)
   - **Redirect URI**: `Web` → `https://g2vpdgb498.localto.net` (dev) or `https://dashboard.publicadjustermidwest.com` (production)
3. Click **Register**
4. On the app's **Overview** page, copy:
   - **Application (client) ID** → `OAUTH_MICROSOFT_CLIENT_ID`
   - **Directory (tenant) ID** → `OAUTH_MICROSOFT_TENANT` (set to `common`)
5. Go to **Certificates & secrets** → **Client secrets** → **New client secret**
   - Copy the **Value** (not ID) → `OAUTH_MICROSOFT_CLIENT_SECRET`
6. Go to **API permissions** → **Add a permission** → **Microsoft Graph** → **Delegated permissions**
   - Add: `IMAP.AccessAsUser.All` (read mail via IMAP)
   - Add: `SMTP.Send` (send mail via SMTP)
   - Add: `offline_access` (refresh tokens — this should be auto-granted)
   - Click **Grant admin consent** (if you're the tenant admin). If not, each user accepts on first login.

### Changing the supported account types on an EXISTING app (this is what the CRM mailbox needs)

The app `Sietch CRM` already exists (client id `e285c2a5-...`). You only need to widen its supported account types:

1. Go to https://entra.microsoft.com and sign in with an account that can manage the app (tenant admin or app owner).
2. Left sidebar: **Identity** → **Applications** → **App registrations**.
3. Click the **Sietch CRM** app.
4. In the app's left blade, click **Authentication**.
5. Under **Supported account types**, select **"Accounts in any organizational directory and personal Microsoft accounts"**.
6. Click **Save** (at the top).
7. Ignore the "Microsoft Entra ID and personal Microsoft accounts" panel that appears — no additional settings needed there.
8. Leave the redirect URIs untouched (they already match the callback).
9. Re-run **Email → Admin → OAuth Connect** in the CRM, sign in as `vanguardadjusting@outlook.com`, and **Accept** the consent screen. The token will then be issued by the consumers tenant (`sts.windows.net/9188040d-...`) and Exchange Online will accept IMAP + SMTP.

> If "Supported account types" is greyed out, the registration may be a legacy/permission issue — in that case register a new app and update the env vars in `.env`.

7. Set env vars:
   ```
   OAUTH_MICROSOFT_CLIENT_ID=your-client-id
   OAUTH_MICROSOFT_CLIENT_SECRET=your-client-secret
   OAUTH_MICROSOFT_TENANT=common
   OAUTH_REDIRECT_URI=https://g2vpdgb498.localto.net
   ```

---

## Google / Gmail

> **Unaffected by the Microsoft changes.** Google is a separate provider class with its own OAuth endpoints and scope (`https://mail.google.com/`); the `common` authority and tenant logic apply only to Microsoft. All provider-agnostic fixes (CRM Mail tab, `is_crm_mail`, `account_id=crm` filter, `SCANNER_SYNC_DAYS`) apply to Google accounts too. OAuth-created Google accounts are also treated as CRM mail accounts (`is_crm_mail = TRUE`).

1. Go to https://console.cloud.google.com/apis/credentials
   - Create a project if needed (e.g. "Sietch CRM")

2. **Enable the Gmail API**:
   - Go to **Library** → search "Gmail API" → **Enable**

3. **Configure OAuth consent screen**:
   - Go to **OAuth consent screen**
   - Choose **External** (or Internal if you use Google Workspace)
   - Fill in: App name (`Sietch CRM`), User support email, Developer contact email
   - **Scopes**: Add `https://mail.google.com/` (full Gmail access) or `https://www.googleapis.com/auth/gmail.imap` (IMAP only)
   - **Test users**: Add your email address
   - Save

4. **Create OAuth client ID**:
   - Go to **Credentials** → **Create credentials** → **OAuth client ID**
   - Application type: **Web application**
   - **Name**: `Sietch CRM`
   - **Authorized redirect URIs**: `https://g2vpdgb498.localto.net` (dev) or `https://dashboard.publicadjustermidwest.com` (production)
   - Click **Create**
   - Copy **Client ID** → `OAUTH_GOOGLE_CLIENT_ID`
   - Copy **Client secret** → `OAUTH_GOOGLE_CLIENT_SECRET`

5. **Note on testing mode**: OAuth consent screen stays in "Testing" mode until you submit for verification. Testing mode expires every 7 days (you'll see "Consent screen is not verified" warnings). This is fine for dev — just renew the expiration when needed. For production, you can submit for verification or limit to your Workspace domain (Internal).

6. Set env vars:
   ```
   OAUTH_GOOGLE_CLIENT_ID=your-client-id
   OAUTH_GOOGLE_CLIENT_SECRET=your-client-secret
   OAUTH_REDIRECT_URI=https://g2vpdgb498.localto.net
   ```

---

## Production deployment

When deploying to production, update `OAUTH_REDIRECT_URI` to the production domain:

```
OAUTH_REDIRECT_URI=https://dashboard.publicadjustermidwest.com
```

This must match **exactly** what you registered in both Entra and Google Cloud. You'll need to add the production URI as an additional redirect URI on both apps (don't remove the dev one — you can have both).

---

## Testing

After setting env vars and restarting the server:

1. Open the Email modal
2. Click the **+** button or an account's edit icon
3. Select **Microsoft 365** or **Google** as the auth method
4. Click **Connect**
5. You'll be redirected to the provider's login page
6. After authorizing, you're redirected back — the account should appear in the list
