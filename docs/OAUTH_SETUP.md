# OAuth 2.0 Provider Setup

You need to register an app with each provider to get `client_id` and `client_secret` for the IMAP OAuth flow.

---

## Microsoft 365 / Entra

1. Go to https://entra.microsoft.com → **App registrations** → **New registration**
2. Fill in:
   - **Name**: `Sietch CRM` (or anything)
   - **Supported account types**: "Accounts in any organizational directory (Any Microsoft ID directory)" — this lets users sign in with any Microsoft 365 work/school account as well as personal Outlook.com accounts. For personal accounts only, pick "Personal Microsoft accounts only".
   - **Redirect URI**: `Web` → `https://g2vpdgb498.localto.net` (dev) or `https://dashboard.publicadjustermidwest.com` (production)
3. Click **Register**
4. On the app's **Overview** page, copy:
   - **Application (client) ID** → `OAUTH_MICROSOFT_CLIENT_ID`
   - **Directory (tenant) ID** → `OAUTH_MICROSOFT_TENANT` (use `common` for multi-tenant)

5. Go to **Certificates & secrets** → **Client secrets** → **New client secret**
   - Copy the **Value** (not ID) → `OAUTH_MICROSOFT_CLIENT_SECRET`

6. Go to **API permissions** → **Add a permission** → **Microsoft Graph** → **Delegated permissions**
   - Add: `IMAP.AccessAsUser.All` (read mail via IMAP)
   - Add: `SMTP.Send` (send mail via SMTP)
   - Add: `offline_access` (refresh tokens — this should be auto-granted)
   - Click **Grant admin consent** (if you're the tenant admin). If not, each user accepts on first login.

7. Set env vars:
   ```
   OAUTH_MICROSOFT_CLIENT_ID=your-client-id
   OAUTH_MICROSOFT_CLIENT_SECRET=your-client-secret
   OAUTH_MICROSOFT_TENANT=common
   OAUTH_REDIRECT_URI=https://g2vpdgb498.localto.net
   ```

---

## Google / Gmail

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
