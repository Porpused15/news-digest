// APK Helper — lists the APKs DroidScript has built and lets you install or share one.
// Android blocks file managers from DroidScript's private folder, but DroidScript itself
// can read it. Create a new *Native* (not Hybrid) app in DroidScript and paste this in.

function OnStart()
{
    // This project lives in .../DroidScript/<project>; built APKs are in .../DroidScript/APKs
    dir = app.GetAppPath().replace( /\/+$/, "" ).replace( /\/[^\/]+$/, "" ) + "/APKs"
    var apks = app.ListFolder( dir, ".apk", 0, "Files,Alphasort" ) || []

    if( !apks.length ) {
        app.Alert( "No APKs found in:\n" + dir, "APK Helper" )
        return
    }

    var dlg = app.CreateListDialog( "Pick an APK", apks.join( "," ) )
    dlg.SetOnTouch( function( name ) { choose( name ) } )
    dlg.Show()
}

function choose( name )
{
    var file = dir + "/" + name
    var act = app.CreateListDialog( name, "Install on this phone,Share (Drive / email / message)" )
    act.SetOnTouch( function( item ) {
        if( item.indexOf( "Install" ) == 0 )
            app.InstallApp( file, function( pkg, status ) { app.ShowPopup( "Install: " + status ) } )
        else
            app.SendFile( file, name, "News Digest app", "Share APK" )
    } )
    act.Show()
}
