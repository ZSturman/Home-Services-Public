// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "HomeServicesConnection",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [.library(name: "HomeServicesConnection", targets: ["HomeServicesConnection"])],
    targets: [.target(name: "HomeServicesConnection"),
              .testTarget(name: "HomeServicesConnectionTests", dependencies: ["HomeServicesConnection"])]
)
