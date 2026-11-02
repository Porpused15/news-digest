// News Digest companion — DroidScript Hybrid app (Material UI "ui" components).
// Create a new Hybrid app in DroidScript and replace its code with this file.
//
// Reading digests needs nothing (the repo is public).
// Changing settings / "Send now" needs a fine-grained GitHub token for this repo only,
// with permissions: Contents = Read and write, Actions = Read and write.

cfg.Portrait

const OWNER = "Porpused15"
const REPO = "news-digest"
const BRANCH = "main"
const WORKFLOW = "digest.yml"
const RAW = `https://raw.githubusercontent.com/${OWNER}/${REPO}/${BRANCH}/`
const API = `https://api.github.com/repos/${OWNER}/${REPO}/`
const STORE = "newsdigest"

const LANGUAGES = ["English", "French"]
const LOOKBACKS = [24, 48, 72]
const W = 0.92       // content width on the Settings tab (leaves side margins)

class Main extends App
{
    onStart()
    {
        this.cfg = null
        this.dates = []
        this.editing = null   // outlet name being edited, or "" when adding

        this.layMain = ui.addLayout( "main", "linear", "fillxy" )
        this.bar = ui.addAppBar( this.layMain, "News Digest", "primary", 1 )
        this.tabs = ui.addTabs( this.layMain, "Digest,Settings", "FullWidth", 1, 0.92 )
        this.tabs.setOnChange( (name) => { if( name == "Settings" && !this.cfg ) this.loadConfig() } )

        this.buildDigestTab( this.tabs.getLayout( "Digest" ) )
        this.buildSettingsTab( this.tabs.getLayout( "Settings" ) )
        this.buildSourceDialog()

        this.loadIndex()
    }

    // ---------------------------------------------------------------- Digest tab

    buildDigestTab( lay )
    {
        lay.setChildMargins( 0.02, 0.01, 0.02, 0.01 )

        const row = ui.addLayout( lay, "linear", "horizontal,vcenter,fillx" )
        this.selDate = ui.addSelect( row, [], "outlined,small", 0.7 )
        this.selDate.label = "Date"
        this.selDate.setOnChange( (value, index) => this.loadDigest( this.dates[ index ] ) )
        this.btnRefresh = ui.addButton( row, "refresh", "icon", 0.2 )
        this.btnRefresh.setOnTouch( () => this.loadIndex() )

        this.layDigest = ui.addLayout( lay, "linear", "scrolly,fillx,center", 1, 0.85 )
    }

    async loadIndex()
    {
        try {
            const idx = await this.getRaw( "digests/index.json" )
            this.dates = idx.dates || []
        }
        catch( e ) {
            this.dates = []
        }
        if( !this.dates.length ) {
            this.layDigest.clear()
            ui.addText( this.layDigest, "No digests yet. They appear here after the next morning run.", "body1,multiline", W )
            return
        }
        this.selDate.setList( this.dates.map( fmtDate ) )
        this.selDate.value = fmtDate( this.dates[0] )
        this.loadDigest( this.dates[0] )
    }

    async loadDigest( date )
    {
        const prd = ui.showProgressDialog( "Loading…" )
        let d
        try {
            d = await this.getRaw( `digests/${date}.json` )
        }
        catch( e ) {
            prd.hide()
            ui.showPopup( "Couldn't load that digest: " + e.message )
            return
        }
        prd.hide()

        const lay = this.layDigest
        lay.clear()
        if( !d.outlets.length )
            ui.addText( lay, "No relevant articles that day.", "body1", W )

        for( const outlet of d.outlets ) {
            ui.addText( lay, outlet.name, "h6", W )
            const lst = ui.addList( lay, outlet.articles.map( a => [ a.title, a.summary ] ), "divider", W )
            lst.setOnTouch( (title, body, icon, action, index) => app.OpenUrl( outlet.articles[ index ].link ) )
        }

        let footer = `${d.outlets.reduce( (n, o) => n + o.articles.length, 0 )} selected from ${d.scanned} articles scanned.`
        if( d.errors && d.errors.length )
            footer += "\nFeeds that failed:\n" + d.errors.join( "\n" )
        ui.addText( lay, footer, "caption,multiline", W )
    }

    // -------------------------------------------------------------- Settings tab

    buildSettingsTab( lay )
    {
        // Children are W wide in a centred layout: gives side margins (setChildMargins
        // didn't apply inside the scroll layout on device).
        const scr = ui.addLayout( lay, "linear", "scrolly,fillxy,center", 1, 1 )
        scr.setChildMargins( 0, 0.012, 0, 0.012 )

        ui.addText( scr, "Delivery", "h6", W )
        this.selHour = ui.addSelect( scr, range( 24 ).map( hourLabel ), "outlined", W )
        this.selHour.label = "Send around"
        this.selLang = ui.addSelect( scr, LANGUAGES, "outlined", W )
        this.selLang.label = "Summary language"
        this.selLook = ui.addSelect( scr, LOOKBACKS.map( h => h + " hours" ), "outlined", W )
        this.selLook.label = "Look back"
        this.swEmail = ui.addSwitch( scr, "Send email", "primary", W )

        ui.addText( scr, "Sources", "h6", W )
        this.lstSources = ui.addList( scr, [], "divider", W )
        this.lstSources.setOnTouch( (title) => this.openSource( title ) )
        this.btnAdd = ui.addButton( scr, "Add source", "outlined", W )
        this.btnAdd.setOnTouch( () => this.openSource( "" ) )

        this.btnSave = ui.addButton( scr, "Save settings", "contained,primary", W )
        this.btnSave.setOnTouch( () => this.saveConfig() )
        this.btnSend = ui.addButton( scr, "Send digest now", "outlined,primary", W )
        this.btnSend.setOnTouch( () => this.sendNow() )

        ui.addText( scr, "GitHub access", "h6", W )
        ui.addText( scr, "Needed to change settings. Reading digests works without it.", "body2,multiline", W )
        this.tfdToken = ui.addTextField( scr, this.token(), "password,outlined", W )
        this.tfdToken.label = "GitHub token (paste here)"
        this.btnToken = ui.addButton( scr, "Save token", "contained,primary", W )
        this.btnToken.setOnTouch( () => {
            clearTimeout( this.tokenTimer )
            this.tokenChanged( true )
        } )
        this.txtToken = ui.addText( scr, "", "body2,multiline", W )
        // Pasting saves automatically; checked once typing/pasting settles.
        this.tfdToken.setOnChange( () => {
            clearTimeout( this.tokenTimer )
            this.tokenTimer = setTimeout( () => this.tokenChanged(), 800 )
        } )
        this.checkToken()
    }

    tokenChanged( force )
    {
        const t = ( this.tfdToken.text || "" ).trim()
        if( t == this.token() && !force ) return
        app.SaveText( "token", t, STORE )
        this.checkToken( true )
    }

    async checkToken( changed )
    {
        const t = this.token()
        if( !t ) {
            this.txtToken.text = "No token saved. Paste one above to change settings."
            return
        }
        this.txtToken.text = "Checking token…"
        try {
            await this.gh( "GET", "" )
            this.txtToken.text = "✓ Token saved and working."
            if( changed ) this.loadConfig()
        }
        catch( e ) {
            this.txtToken.text = e.expired
                ? "✗ Token expired or invalid. Create a new one on GitHub and paste it above."
                : "Couldn't check the token (" + e.message + "). It's saved; try again when online."
        }
    }

    async loadConfig()
    {
        try {
            // With a token, read through the API (always current); otherwise the public file.
            this.cfg = this.token() ? (await this.getContents( "config.json" )).data
                                    : await this.getRaw( "config.json" )
        }
        catch( e ) {
            ui.showPopup( "Couldn't load settings: " + e.message )
            return
        }
        const c = this.cfg
        this.selHour.value = hourLabel( c.send_hour ?? 6 )
        this.selLang.value = c.summary_language || "English"
        this.selLook.value = ( c.lookback_hours || 24 ) + " hours"
        this.swEmail.value = c.email_enabled !== false
        this.showSources()
    }

    showSources()
    {
        this.lstSources.setList( Object.entries( this.cfg.feeds ).map(
            ([ name, urls ]) => [ name, urls.length + ( urls.length == 1 ? " feed" : " feeds" ) ] ) )
    }

    async saveConfig()
    {
        if( !this.cfg ) return ui.showPopup( "Settings not loaded yet" )
        if( !this.token() ) return ui.showPopup( "Add a GitHub token first" )

        // On device, Select.value comes back as an array (["6:00 AM"]); take the item.
        const hour = range( 24 ).map( hourLabel ).indexOf( selValue( this.selHour ) )
        const lang = selValue( this.selLang )
        const look = parseInt( selValue( this.selLook ) )
        if( hour < 0 || !LANGUAGES.includes( lang ) || !LOOKBACKS.includes( look ) )
            return ui.showPopup( "Couldn't read the settings fields; nothing was saved" )
        this.cfg.send_hour = hour
        this.cfg.summary_language = lang
        this.cfg.lookback_hours = look
        this.cfg.email_enabled = !!this.swEmail.value

        const prd = ui.showProgressDialog( "Saving…" )
        try {
            const { sha } = await this.getContents( "config.json" )
            await this.gh( "PUT", "contents/config.json", {
                message: "Update settings from app",
                content: b64encode( JSON.stringify( this.cfg, null, 2 ) + "\n" ),
                sha, branch: BRANCH
            } )
            ui.showPopup( "Settings saved" )
        }
        catch( e ) {
            ui.showPopup( "Save failed: " + e.message )
        }
        prd.hide()
    }

    async sendNow()
    {
        if( !this.token() ) return ui.showPopup( "Add a GitHub token first" )
        try {
            await this.gh( "POST", `actions/workflows/${WORKFLOW}/dispatches`, { ref: BRANCH } )
            ui.showPopup( "Started. The email arrives in a few minutes." )
        }
        catch( e ) {
            ui.showPopup( "Couldn't start: " + e.message )
        }
    }

    // ------------------------------------------------------------ Source dialog

    buildSourceDialog()
    {
        this.dlg = ui.addDialog( "Source", "", [ "Delete", "Done" ] )
        // No width on dialog fields (as in the official dialog sample): the dialog's inner
        // layout is wider than the visible dialog, so fractional widths overflow.
        const lay = this.dlg.layout
        this.tfdName = ui.addTextField( lay, "", "outlined" )
        this.tfdName.label = "Name"
        this.tfdUrls = ui.addTextField( lay, "", "outlined,multiline" )
        this.tfdUrls.label = "Feed URLs (one per line)"
        this.tfdUrls.setRows( 3, 8 )
        this.dlg.setOnAction( (text) => this.sourceAction( text ) )
    }

    openSource( name )
    {
        if( !this.cfg ) return ui.showPopup( "Settings not loaded yet" )
        this.editing = name
        this.dlg.title = name ? "Edit source" : "Add source"
        this.tfdName.text = name
        this.tfdUrls.text = name ? this.cfg.feeds[ name ].join( "\n" ) : ""
        this.dlg.show()
    }

    sourceAction( action )
    {
        const feeds = this.cfg.feeds
        if( action == "Delete" ) {
            if( this.editing ) delete feeds[ this.editing ]
        }
        else {
            const name = this.tfdName.text.trim()
            const urls = this.tfdUrls.text.split( /\s+/ ).filter( u => /^https?:\/\//.test( u ) )
            if( !name || !urls.length ) return ui.showPopup( "Need a name and at least one http(s) URL" )
            // Rebuild to keep the outlet's position when renaming.
            const out = {}
            let placed = false
            for( const [ k, v ] of Object.entries( feeds ) ) {
                if( k == this.editing ) { out[ name ] = urls; placed = true }
                else if( k != name ) out[ k ] = v
            }
            if( !placed ) out[ name ] = urls
            this.cfg.feeds = out
        }
        this.showSources()
        ui.showPopup( "Changed. Tap Save settings to apply." )
    }

    // ----------------------------------------------------------------- Network

    token() { return ( app.LoadText( "token", "", STORE ) || "" ).trim() }

    async getRaw( path )
    {
        const r = await fetch( RAW + path + "?t=" + Date.now(), { cache: "no-store" } )
        if( !r.ok ) throw new Error( "HTTP " + r.status )
        return r.json()
    }

    async getContents( path )
    {
        const j = await this.gh( "GET", `contents/${path}?ref=${BRANCH}` )
        return { sha: j.sha, data: JSON.parse( b64decode( j.content ) ) }
    }

    async gh( method, path, body )
    {
        const r = await fetch( path ? API + path : API.slice( 0, -1 ), {
            method,
            headers: {
                "Accept": "application/vnd.github+json",
                "Authorization": "Bearer " + this.token(),
                "X-GitHub-Api-Version": "2022-11-28",
                ...( body ? { "Content-Type": "application/json" } : {} )
            },
            body: body ? JSON.stringify( body ) : undefined
        } )
        if( r.status == 401 ) {
            if( this.txtToken ) this.txtToken.text = "✗ Token expired or invalid. Create a new one on GitHub and paste it above."
            throw Object.assign( new Error( "token expired or invalid" ), { expired: true } )
        }
        if( r.status == 403 || r.status == 404 ) throw new Error( "token lacks access to this repo (HTTP " + r.status + ")" )
        if( !r.ok ) throw new Error( "HTTP " + r.status + " " + ( await r.text() ).slice( 0, 120 ) )
        return r.status == 204 ? null : r.json()
    }
}

// ------------------------------------------------------------------- Helpers

function selValue( sel )
{
    const v = sel.value
    return Array.isArray( v ) ? v[0] : v
}

function range( n ) { return Array.from( { length: n }, (_, i) => i ) }

function hourLabel( h )
{
    return ( h % 12 || 12 ) + ":00 " + ( h < 12 ? "AM" : "PM" )
}

function fmtDate( iso )
{
    return new Date( iso + "T12:00:00" ).toLocaleDateString( "en-CA",
        { weekday: "short", month: "short", day: "numeric" } )
}

function b64encode( str )
{
    const bytes = new TextEncoder().encode( str )
    let bin = ""
    for( const b of bytes ) bin += String.fromCharCode( b )
    return btoa( bin )
}

function b64decode( b64 )
{
    const bin = atob( b64.replace( /\s/g, "" ) )
    return new TextDecoder().decode( Uint8Array.from( bin, c => c.charCodeAt( 0 ) ) )
}
