// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "appdriver",
    platforms: [
        .macOS(.v14)
    ],
    targets: [
        .executableTarget(
            name: "appdriver",
            path: "Sources/appdriver"
        )
    ]
)
