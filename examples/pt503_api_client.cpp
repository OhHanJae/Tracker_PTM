#include "pt503_api_client.h"

#include <QJsonDocument>

namespace {
constexpr qsizetype kMaximumLineBytes = 65536;
}

Pt503ApiClient::Pt503ApiClient(QObject *parent)
    : QObject(parent)
{
    connect(&socket_, &QTcpSocket::connected,
            this, &Pt503ApiClient::serviceConnected);
    connect(&socket_, &QTcpSocket::disconnected,
            this, &Pt503ApiClient::serviceDisconnected);
    connect(&socket_, &QTcpSocket::readyRead,
            this, &Pt503ApiClient::readAvailableLines);
    connect(&socket_, &QTcpSocket::errorOccurred, this,
            [this](QAbstractSocket::SocketError) {
                emit protocolError(socket_.errorString());
            });
}

void Pt503ApiClient::connectToService(quint16 port)
{
    // 서비스는 같은 PC의 IPv4 localhost만 수신합니다.
    socket_.connectToHost(QStringLiteral("127.0.0.1"), port);
}

void Pt503ApiClient::disconnectFromService()
{
    socket_.disconnectFromHost();
}

QString Pt503ApiClient::sendCommand(const QString &command,
                                    const QJsonObject &params)
{
    const QString requestId = QStringLiteral("qt-%1").arg(nextRequestId_++);
    const QJsonObject request{
        {QStringLiteral("id"), requestId},
        {QStringLiteral("command"), command},
        {QStringLiteral("params"), params},
    };
    socket_.write(QJsonDocument(request).toJson(QJsonDocument::Compact));
    socket_.write("\n");
    return requestId;
}

void Pt503ApiClient::readAvailableLines()
{
    receiveBuffer_.append(socket_.readAll());
    if (receiveBuffer_.size() > kMaximumLineBytes
        && !receiveBuffer_.contains('\n')) {
        emit protocolError(QStringLiteral("API 프레임이 65536바이트를 초과했습니다."));
        socket_.abort();
        return;
    }

    qsizetype newline = -1;
    while ((newline = receiveBuffer_.indexOf('\n')) >= 0) {
        QByteArray line = receiveBuffer_.left(newline).trimmed();
        receiveBuffer_.remove(0, newline + 1);
        if (line.isEmpty())
            continue;

        QJsonParseError parseError;
        const QJsonDocument document = QJsonDocument::fromJson(line, &parseError);
        if (parseError.error != QJsonParseError::NoError || !document.isObject()) {
            emit protocolError(parseError.errorString());
            continue;
        }
        const QJsonObject message = document.object();
        if (message.contains(QStringLiteral("event"))) {
            emit eventReceived(
                message.value(QStringLiteral("event")).toString(),
                message.value(QStringLiteral("data")).toObject(),
                message.value(QStringLiteral("timestamp")).toString());
            continue;
        }

        const QString requestId =
            message.value(QStringLiteral("id")).toVariant().toString();
        if (message.value(QStringLiteral("ok")).toBool()) {
            emit responseReceived(
                requestId, message.value(QStringLiteral("result")).toObject());
        } else {
            emit errorReceived(
                requestId, message.value(QStringLiteral("error")).toObject());
        }
    }
}
