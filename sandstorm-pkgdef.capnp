@0xc3217fd74cd95974;

using Spk = import "/sandstorm/package.capnp";

const pkgdef :Spk.PackageDefinition = (
  id = "dhuy91jv0ap1wf5qcnjncuy53pt2ajcz4v5mh0sr14deu3k69kvh",

  manifest = (
    appVersion = 0,

    actions = [
      ( title = (defaultText = "New Mailpile Mailbox"),
        command = .myCommand
      )
    ],

    continueCommand = .myCommand
  ),

  sourceMap = (
    searchPath = [
      ( sourcePath = "." ),
      # Include source directory.

      ( sourcePath = "/opt/sandstorm/latest/usr/include/sandstorm" ),
      # Include Sandstorm protocol schemas (especially email.capnp).

      ( sourcePath = "/bin/busybox", packagePath = "bin/sh" ),
      # Map bin/sh to busybox.

      ( sourcePath = "/", hidePaths = [ "home", "proc", "sys" ] )
      # Map root dir.
    ]
  ),

  fileList = "sandstorm-files.list",
  alwaysInclude = [ "mailpile", "plugins", "static" ]
);

const myCommand :Spk.Manifest.Command = (
  argv = ["/sandstorm-http-bridge", "33411", "--", "/run-sandstorm.sh"],
  environ = [
    (key = "PATH", value = "/usr/local/bin:/usr/bin:/bin")
  ]
);
