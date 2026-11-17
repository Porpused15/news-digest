// Service.js: background check for the morning digest (DroidScript service).
// Put this file next to the main app file in the same DroidScript project; the main app
// starts it with app.CreateService( "this", "this", ... ).
//
// Once a day: at CHECK_TIME the service looks for today's date in digests/index.json. If
// it's there, it posts one notification ("Today's digest: N stories", top headlines);
// tapping it brings the app to the front. If the digest isn't there yet (slow Gemini, the
// hourly retry), it checks again every RETRY_MIN minutes until LAST_HOUR, then waits for
// tomorrow. Nothing is posted if the app already showed today's digest, or if the
// "Morning notification" switch in the app is off.
//
// Alarms set from a service call back to the service (DroidScript release notes).
// Written in plain ES5: services don't run in the Hybrid app's WebView.

var BASE = "https://raw.githubusercontent.com/Porpused15/news-digest/main/digests/"
var STORE = "newsdigest"            // same store as the main app
var CHECK_TIME = [ 6, 40 ]          // local time; the digest normally lands around 6:25
var RETRY_MIN = 30
var LAST_HOUR = 12
var ALARM_ID = 4201

function OnStart()
{
    // (Re)arm on every start: app opened, or restarted by Android. Inside the morning
    // window, check in 2 minutes rather than now, so an app that's just been opened has
    // marked today's digest as read first (no alert about what's already on screen).
    var now = new Date()
    if( inCheckWindow( now ) && !doneToday() ) schedule( new Date( now.getTime() + 2 * 60000 ) )
    else schedule( nextMorning( now ) )
}

// Main app sends "check" when the switch is turned on, "rearm" after settings change.
function OnMessage( msg )
{
    if( msg == "check" ) check()
    else schedule( nextMorning( new Date() ) )
}

function OnAlarm( id )
{
    check()
}

function check()
{
    var now = new Date()
    if( !enabled() || doneToday() ) return schedule( nextMorning( now ) )
    var today = isoDate( now )
    getJson( BASE + "index.json", function( idx ) {
        var dates = ( idx && idx.dates ) || []
        if( dates.indexOf( today ) < 0 ) {
            // Not there yet: retry within the window, else try again tomorrow.
            var next = new Date( now.getTime() + RETRY_MIN * 60000 )
            return schedule( next.getHours() < LAST_HOUR ? next : nextMorning( now ) )
        }
        getJson( BASE + today + ".json", function( d ) {
            notify( d )
            app.SaveText( "notified", today, STORE )
            schedule( nextMorning( now ) )
        } )
    } )
}

function notify( d )
{
    var stories = ( d && d.stories ) || []
    var title = !d ? "Today's digest is ready"
              : "Today's digest: " + stories.length + ( stories.length == 1 ? " story" : " stories" )
    var top = []
    for( var i = 0; i < stories.length && i < 4; i++ ) top.push( "• " + stories[i].headline )
    var ntf = app.CreateNotification( "AutoCancel" )
    ntf.SetMessage( title, title, top.length ? top[0].slice( 2 ) : "Tap to read", top.join( "\n" ) )
    ntf.Notify( "digest" )
}

// --------------------------------------------------------------------- helpers

function getJson( url, done )
{
    app.HttpRequest( "GET", url + "?t=" + Date.now(), null, null, function( error, reply, status ) {
        var obj = null
        if( !error && status == 200 ) { try { obj = JSON.parse( reply ) } catch( e ) {} }
        if( obj === null ) {
            // Network or parse failure: same as "not there yet" for the index; for the
            // digest file, still notify with what we know.
            app.Debug( "digest check: " + url + " -> " + status )
        }
        done( obj )
    } )
}

function schedule( when )
{
    app.SetAlarm( "Set", ALARM_ID, OnAlarm, when.getTime(), null, "ExactIdle" )
}

function nextMorning( now )
{
    var t = new Date( now.getFullYear(), now.getMonth(), now.getDate(), CHECK_TIME[0], CHECK_TIME[1] )
    if( t.getTime() <= now.getTime() ) t.setDate( t.getDate() + 1 )
    return t
}

function inCheckWindow( now )
{
    var mins = now.getHours() * 60 + now.getMinutes()
    return mins >= CHECK_TIME[0] * 60 + CHECK_TIME[1] && now.getHours() < LAST_HOUR
}

function doneToday() { return app.LoadText( "notified", "", STORE ) == isoDate( new Date() ) }

function enabled() { return app.LoadText( "notify_on", "1", STORE ) == "1" }

function isoDate( d )
{
    return d.getFullYear() + "-" + pad( d.getMonth() + 1 ) + "-" + pad( d.getDate() )
}

function pad( n ) { return ( n < 10 ? "0" : "" ) + n }
