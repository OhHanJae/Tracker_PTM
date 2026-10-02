#pragma once

#include <QJsonObject>
#include <QObject>
#include <QTcpSocket>

// Qt 6 Main APP용 비동기 클라이언트입니다. 요청 결과와 장비 이벤트는
// 각각 responseReceived/errorReceived/eventReceived 시그널로 전달됩니다.
class Pt503ApiClient final : public QObject
{
    Q_OBJECT

public:
    explicit Pt503ApiClient(QObject *parent = nullptr);

    void connectToService(quint16 port = 8765);
    void disconnectFromService();
    QString sendCommand(const QString &command,
                        const QJsonObject &params = QJsonObject());

signals:
    void serviceConnected();
    void serviceDisconnected();
    void responseReceived(const QString &requestId, const QJsonObject &result);
    void errorReceived(const QString &requestId, const QJsonObject &error);
    void eventReceived(const QString &eventName, const QJsonObject &data,
                       const QString &timestamp);
    void protocolError(const QString &message);

private slots:
    void readAvailableLines();

private:
    QTcpSocket socket_;
    QByteArray receiveBuffer_;
    quint64 nextRequestId_ = 1;
};
